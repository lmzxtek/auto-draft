# Each `paper` is a dictionary containing:
#       (1) paper_id (2) title (3) authors (4) year (5) link (6) abstract (7) journal
#
# Generate references:
#   `Reference` class:
#       1. Read a given .bib file to collect papers; use `search_paper_abstract` method to fill missing abstract.
#       2. Given some keywords; use ArXiv or Semantic Scholar API to find papers.
#       3. Generate bibtex from the selected papers. --> to_bibtex()
#       4. Generate prompts from the selected papers: --> to_prompts()
#               A sample prompt: {"paper_id": "paper summary"}

import requests
import re
import bibtexparser
from scholarly import scholarly
from scholarly import ProxyGenerator


######################################################################################################################
# Some basic tools
######################################################################################################################
def remove_newlines(serie):
    # This function is applied to the abstract of each paper to reduce the length of prompts.
    serie = serie.replace('\n', ' ')
    serie = serie.replace('\\n', ' ')
    serie = serie.replace('  ', ' ')
    serie = serie.replace('  ', ' ')
    return serie


def search_paper_abstract(title):
    pg = ProxyGenerator()
    success = pg.ScraperAPI("921b16f94d701308b9d9b4456ddde155")
    scholarly.use_proxy(pg)
    # input the title of a paper, return its abstract
    search_query = scholarly.search_pubs(title)
    paper = next(search_query)
    return remove_newlines(paper['bib']['abstract'])


def load_papers_from_bibtex(bib_file_path):
    with open(bib_file_path) as bibtex_file:
        bib_database = bibtexparser.load(bibtex_file)
    if len(bib_database.entries) == 0:
        return []
    else:
        bib_papers = []
        for bibitem in bib_database.entries:
            paper_id = bibitem.get("ID")
            title = bibitem.get("title")
            if title is None:
                continue
            journal = bibitem.get("journal")
            year = bibitem.get("year")
            author = bibitem.get("author")
            abstract = bibitem.get("abstract")
            if abstract is None:
                abstract = search_paper_abstract(title)
            result = {
                "paper_id": paper_id,
                "title": title,
                "link": "",
                "abstract": abstract,
                "authors": author,
                "year": year,
                "journal": journal
            }
            bib_papers.append(result)
        return bib_papers


######################################################################################################################
# Semantic Scholar (SS) API
######################################################################################################################
def ss_search(keywords, limit=20, fields=None):
    # space between the  query to be removed and replaced with +
    if fields is None:
        fields = ["title", "abstract", "venue", "year", "authors", "tldr", "embedding", "externalIds"]
    keywords = keywords.lower()
    keywords = keywords.replace(" ", "+")
    url = f'https://api.semanticscholar.org/graph/v1/paper/search?query={keywords}&limit={limit}&fields={",".join(fields)}'
    # headers = {"Accept": "*/*", "x-api-key": constants.S2_KEY}
    headers = {"Accept": "*/*"}

    response = requests.get(url, headers=headers, timeout=30)
    return response.json()


def _collect_papers_ss(keyword, counts=3, tldr=False):
    def externalIds2link(externalIds):
        # Sample externalIds:
        #   "{'MAG': '2932819148', 'DBLP': 'conf/icml/HaarnojaZAL18', 'ArXiv': '1801.01290', 'CorpusId': 28202810}"
        if externalIds:
            # Supports ArXiv, MAG, ACL, PubMed, Medline, PubMedCentral, DBLP, DOI
            # priority: DBLP > arXiv > (todo: MAG > CorpusId > DOI > ACL > PubMed > Mdeline > PubMedCentral)
            # DBLP
            dblp_id = externalIds.get('DBLP')
            if dblp_id is not None:
                dblp_link = f"dblp.org/rec/{dblp_id}"
                return dblp_link
            # arXiv
            arxiv_id = externalIds.get('ArXiv')
            if arxiv_id is not None:
                arxiv_link = f"arxiv.org/abs/{arxiv_id}"
                return arxiv_link
            return ""
        else:
            # if this is an empty dictionary, return an empty string
            return ""

    def extract_paper_id(last_name, year_str, title):
        pattern = r'^\w+'
        words = re.findall(pattern, title)
        # return last_name + year_str + title.split(' ', 1)[0]
        try:
            output = last_name + year_str + words[0]
        except IndexError:
            output = last_name + year_str + title[:4]
        return output

    def extract_author_info(raw_authors):
        authors = [author['name'] for author in raw_authors]

        authors_str = " and ".join(authors)
        try:
            last_name = authors[0].split()[-1]
        except IndexError:
            last_name = "ma"
        # pattern = r'^\w+'
        # last_name = re.findall(pattern, authors[0])
        return authors_str, last_name

    def parse_search_results(search_results_ss):
        # turn the search result to a list of paper dictionary.
        papers_ss = []
        for raw_paper in search_results_ss:
            if raw_paper["abstract"] is None:
                continue

            authors_str, last_name = extract_author_info(raw_paper['authors'])
            year_str = str(raw_paper['year'])
            title = raw_paper['title']
            # some journal may contain &; replace it. e.g. journal={IEEE Power & Energy Society General Meeting}
            journal = raw_paper['venue'].replace("&", "\\&")
            if not journal:
                journal = "arXiv preprint"
            paper_id = extract_paper_id(last_name, year_str, title).lower()
            link = externalIds2link(raw_paper['externalIds'])
            if tldr and raw_paper['tldr'] is not None:
                abstract = raw_paper['tldr']['text']
            else:
                abstract = remove_newlines(raw_paper['abstract'])
            result = {
                "paper_id": paper_id,
                "title": title,
                "abstract": abstract,
                "link": link,
                "authors": authors_str,
                "year": year_str,
                "journal": journal
            }
            papers_ss.append(result)
        return papers_ss

    raw_results = ss_search(keyword, limit=counts)
    if raw_results is not None:
        search_results = raw_results['data']
    else:
        search_results = []
    results = parse_search_results(search_results)
    return results


######################################################################################################################
# ArXiv API
######################################################################################################################
def _collect_papers_arxiv(keyword, counts=3, tldr=False):
    # Build the arXiv API query URL with the given keyword and other parameters
    def build_query_url(keyword, results_limit=3, sort_by="relevance", sort_order="descending"):
        base_url = "http://export.arxiv.org/api/query?"
        query = f"search_query=all:{keyword}&start=0&max_results={results_limit}"
        query += f"&sortBy={sort_by}&sortOrder={sort_order}"
        return base_url + query

    # Fetch search results from the arXiv API using the constructed URL
    def fetch_search_results(query_url):
        response = requests.get(query_url)
        return response.text

    # Parse the XML content of the API response to extract paper information
    def parse_results(content):
        from xml.etree import ElementTree as ET

        root = ET.fromstring(content)
        namespace = "{http://www.w3.org/2005/Atom}"
        entries = root.findall(f"{namespace}entry")

        results = []
        for entry in entries:
            title = entry.find(f"{namespace}title").text
            link = entry.find(f"{namespace}id").text
            summary = entry.find(f"{namespace}summary").text
            summary = remove_newlines(summary)

            # Extract the authors
            authors = entry.findall(f"{namespace}author")
            author_list = []
            for author in authors:
                name = author.find(f"{namespace}name").text
                author_list.append(name)
            authors_str = " and ".join(author_list)

            # Extract the year
            published = entry.find(f"{namespace}published").text
            year = published.split("-")[0]

            founds = re.search(r'\d+\.\d+', link)
            if founds is None:
                # some links are not standard; such as "https://arxiv.org/abs/cs/0603127v1".
                # will be solved in the future.
                continue
            else:
                arxiv_id = founds.group(0)
            journal = f"arXiv preprint arXiv:{arxiv_id}"
            result = {
                "paper_id": arxiv_id,
                "title": title,
                "link": link,
                "abstract": summary,
                "authors": authors_str,
                "year": year,
                "journal": journal
            }
            results.append(result)

        return results

    query_url = build_query_url(keyword, counts)
    content = fetch_search_results(query_url)
    results = parse_results(content)
    return results


######################################################################################################################
# References Class
######################################################################################################################

class References:
    def __init__(self, load_papers=""):
        if load_papers:
            # todo: (1) too large bibtex may make have issues on token limitations; may truncate to 5 or 10
            #       (2) google scholar didn't give a full abstract for some papers ...
            #       (3) may use langchain to support long input
            self.papers = load_papers_from_bibtex(load_papers)
        else:
            self.papers = []

    def collect_papers(self, keywords_dict, method="arxiv", tldr=False):
        """
        keywords_dict:
            {"machine learning": 5, "language model": 2};
            the first is the keyword, the second is how many references are needed.
        """
        match method:
            case "arxiv":
                process = _collect_papers_arxiv
            case "ss":
                process = _collect_papers_ss
            case _:
                raise NotImplementedError("Other sources have not been not supported yet.")
        for key, counts in keywords_dict.items():
            self.papers = self.papers + process(key, counts, tldr)

        seen = set()
        papers = []
        for paper in self.papers:
            paper_id = paper["paper_id"]
            if paper_id not in seen:
                seen.add(paper_id)
                papers.append(paper)
        self.papers = papers

    def to_bibtex(self, path_to_bibtex="ref.bib"):
        """
        Turn the saved paper list into bibtex file "ref.bib". Return a list of all `paper_id`.
        """
        papers = self.papers

        # clear the bibtex file
        with open(path_to_bibtex, "w", encoding="utf-8") as file:
            file.write("")

        bibtex_entries = []
        paper_ids = []
        for paper in papers:
            bibtex_entry = f"""@article{{{paper["paper_id"]},
          title = {{{paper["title"]}}},
          author = {{{paper["authors"]}}}, 
          journal={{{paper["journal"]}}}, 
          year = {{{paper["year"]}}}, 
          url = {{{paper["link"]}}}
        }}"""
            bibtex_entries.append(bibtex_entry)
            paper_ids.append(paper["paper_id"])
            # Save the generated BibTeX entries to a file
            with open(path_to_bibtex, "a", encoding="utf-8") as file:
                file.write(bibtex_entry)
                file.write("\n\n")
        return paper_ids

    def to_prompts(self):
        # `prompts`:
        #   {"paper1_bibtex_id": "paper_1_abstract", "paper2_bibtex_id": "paper2_abstract"}
        #   this will be used to instruct GPT model to cite the correct bibtex entry.
        prompts = {}
        for paper in self.papers:
            prompts[paper["paper_id"]] = paper["abstract"]
        return prompts


if __name__ == "__main__":
    # refs = References()
    # keywords_dict = {
    #     "Deep Q-Networks": 15,
    #     "Policy Gradient Methods": 24,
    #     "Actor-Critic Algorithms": 4,
    #     "Model-Based Reinforcement Learning": 13,
    #     "Exploration-Exploitation Trade-off": 7
    # }
    # refs.collect_papers(keywords_dict, method="ss", tldr=True)
    # for p in refs.papers:
    #     print(p["paper_id"])
    # print(len(refs.papers))

    bib = "D:\\Projects\\auto-draft\\latex_templates\\pre_refs.bib"
    papers = load_papers_from_bibtex(bib)
    for paper in papers:
        print(paper)
