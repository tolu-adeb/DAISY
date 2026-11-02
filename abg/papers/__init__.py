"""Research papers: read a paper you added on the website and turn its testable parts into forward-lab ideas.

Flow (all outbound from this computer; the website only stores what you add and what the bot reports):

    website  /app/ "Research papers"  you add a PDF, an arXiv ID, a DOI, a link or a title, then press Analyse paper
             -> a command signed with your control code: analyze_paper {paper_id, sha256}
    bot      SitePublisher pulls the command, checks the HMAC, then PaperDesk (desk.py):
               fetch.py    downloads the PDF from the site (or arXiv / an open-access link), with size, time and
                           address limits; sha256 must match the command, so a view key alone can't spend tokens
               extract.py  PDF text by section (pypdf); references dropped; cut to fit the per-paper token cap
               analyse.py  one Claude call -> JSON validated with Pydantic; testable ideas must be valid
                           ``Hypothesis`` objects (search-space knobs only, never the loss controls)
             -> pushes status and the analysis back to the site (signed ``papers`` ingest)
    you      Test in forward lab -> queue.py adds the idea to data/papers/queue.json; the forward lab tests it on its
             next run like any other idea (walk-forward gate, then paper trading on unseen sessions)

Nothing here posts to Discord or changes the live settings.  Approving a strategy stays a separate, manual step.
"""

PAPERS_DIR = "papers"
