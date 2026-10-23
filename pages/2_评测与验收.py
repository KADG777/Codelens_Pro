"""Saved evaluation evidence, separate from the workflow workspace."""

import streamlit as st

from review_agent.config import ROOT
from ui.comparison_view import render_comparison
from ui.evaluation_view import render_evaluation_page

st.set_page_config(page_title="评测与验收 · CodeLens Flow", page_icon="📊", layout="wide")
comparison, history = st.tabs(["同任务对照实验", "流程历史评测"])
with comparison:
    render_comparison(ROOT)
with history:
    render_evaluation_page()
