"""The indexing pipeline: understands each saved item once, when it is saved.

`docs/ARCHITECTURE.md` §3.3. One arq job per saved item (`worker.py`) runs the
steps in `indexer.py`: fetch the media, classify, speech-to-text, extract (with
the video escalation), normalize open values against the vocabulary, embed, and
send the save confirmation. Each step's result is kept in `enrichment_run`, so a
job that is tried again continues where the last try stopped.
"""
