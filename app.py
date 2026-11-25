"""Streamlit web UI:  streamlit run app.py"""
import streamlit as st

from rag.engine import Engine, LLMError
from rag.ingest import QUARANTINED

st.set_page_config(page_title="KA Document Assistant")
st.title("KA Document Assistant")
st.caption("Answers come only from the supplied documents, with citations.")


@st.cache_resource
def get_engine() -> Engine:
    return Engine()  # indexes source_docs/ (or SOURCE_DIR)


engine = get_engine()
with st.sidebar:
    st.write(f"Indexed {len(engine.chunks)} chunks from:")
    for d in sorted({c.doc for c in engine.chunks}):
        st.write(f"- {d}")
    if QUARANTINED:
        st.warning(f"{len(QUARANTINED)} line(s) removed at ingest as suspected prompt injection.")
        for doc, loc, line in QUARANTINED:
            st.caption(f"{doc} > {loc}: \"{line}\"")

q = st.text_input("Ask a question")
if q:
    with st.spinner("Searching..."):
        try:
            res = engine.ask(q)
        except LLMError as e:
            st.error(str(e))
            st.stop()
    if res["found"]:
        st.markdown(res["answer"])
        st.subheader("Sources")
        for s in res["sources"]:
            with st.expander(f"[{s['n']}] {s['citation']}"):
                st.text(s["text"])
    else:
        st.warning(res["answer"])
