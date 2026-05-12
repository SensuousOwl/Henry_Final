# =========================
# LegalMove - Contract Comparator (PRO VERSION)
# =========================

import os
import base64
import json
from typing import List, Optional
from dotenv import load_dotenv

import streamlit as st
from pydantic import BaseModel, ValidationError

import pymupdf as fitz

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate

from langfuse import Langfuse

# PDF
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from io import BytesIO

# =========================
# SESSION STATE INIT
# =========================
if "analysis_done" not in st.session_state:
    st.session_state.analysis_done = False
    st.session_state.result = None
    st.session_state.metrics = None
    st.session_state.json_str = None
    st.session_state.pdf_buffer = None

# =========================
# ENV
# =========================
load_dotenv()

langfuse = Langfuse(
    public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
    secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
    host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
)

# =========================
# TOKEN TRACKING
# =========================
token_usage = {
    "input": 0,
    "output": 0
}

def track_tokens(res):
    try:
        usage = res.response_metadata["token_usage"]
        token_usage["input"] += usage.get("prompt_tokens", 0)
        token_usage["output"] += usage.get("completion_tokens", 0)
    except:
        pass

def estimate_cost():
    input_cost = token_usage["input"] * 0.00000015
    output_cost = token_usage["output"] * 0.0000006

    return {
        "input_tokens": token_usage["input"],
        "output_tokens": token_usage["output"],
        "total_tokens": token_usage["input"] + token_usage["output"],
        "estimated_cost_usd": round(input_cost + output_cost, 6)
    }

# =========================
# LOGGING
# =========================
def log(name, data):
    try:
        trace = langfuse.start_trace(name=name)
        trace.end(output={"result": str(data)[:3000]})
    except:
        pass

# =========================
# SCHEMA
# =========================
class ClauseChange(BaseModel):
    clause_id: Optional[str]
    original_text: str
    amended_text: str
    change_type: str
    legal_impact: str

class ContractDiff(BaseModel):
    total_changes: int
    modified_clauses: List[ClauseChange]
    affected_topics: List[str]
    summary: str

# =========================
# UTILS
# =========================
def encode_bytes(b: bytes):
    return base64.b64encode(b).decode("utf-8")

# =========================
# PDF GENERATOR
# =========================
def generate_pdf(result_dict, metrics_dict):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer)

    styles = getSampleStyleSheet()
    content = []

    content.append(Paragraph("LegalMove AI - Contract Analysis", styles["Title"]))
    content.append(Spacer(1, 12))

    content.append(Paragraph("Summary:", styles["Heading2"]))
    content.append(Paragraph(result_dict["summary"], styles["BodyText"]))
    content.append(Spacer(1, 12))

    content.append(Paragraph(f"Total Changes: {result_dict['total_changes']}", styles["BodyText"]))
    content.append(Spacer(1, 12))

    content.append(Paragraph("Modified Clauses:", styles["Heading2"]))

    for clause in result_dict["modified_clauses"]:
        text = f"""
        Clause ID: {clause.get('clause_id')}<br/>
        Type: {clause.get('change_type')}<br/>
        Impact: {clause.get('legal_impact')}<br/>
        <br/>
        Original: {clause.get('original_text')}<br/>
        Amended: {clause.get('amended_text')}<br/>
        """
        content.append(Paragraph(text, styles["BodyText"]))
        content.append(Spacer(1, 12))

    content.append(Paragraph("Affected Topics:", styles["Heading2"]))
    content.append(Paragraph(", ".join(result_dict["affected_topics"]), styles["BodyText"]))
    content.append(Spacer(1, 12))

    content.append(Paragraph("Metrics:", styles["Heading2"]))
    content.append(Paragraph(json.dumps(metrics_dict, indent=2), styles["BodyText"]))

    doc.build(content)
    buffer.seek(0)

    return buffer

# =========================
# PARSING
# =========================
def parse_image(image_b64):
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        model_kwargs={"response_format": {"type": "json_object"}}
    )

    msg = HumanMessage(content=[
        {"type": "text", "text": "Extract all text. Return JSON: {\"text\":\"...\"}"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
    ])

    res = llm.invoke([msg])
    track_tokens(res)

    try:
        return json.loads(res.content)["text"]
    except:
        return res.content

def parse_pdf(file):
    doc = fitz.open(stream=file.read(), filetype="pdf")
    text = ""

    for page in doc:
        t = page.get_text("text")
        if t.strip():
            text += "\n" + t
        else:
            pix = page.get_pixmap()
            text += "\n" + parse_image(encode_bytes(pix.tobytes("png")))

    return text

def extract_text(file):
    if file.type == "application/pdf":
        return parse_pdf(file)
    return parse_image(encode_bytes(file.read()))

# =========================
# AGENTS
# =========================
def build_agents():

    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        model_kwargs={"response_format": {"type": "json_object"}}
    )

    structure_prompt = ChatPromptTemplate.from_template("""
    Structure contract into clauses.

    Return JSON:
    {{
        "clauses":[{{"id":"1","title":"...","text":"..."}}]
    }}

    CONTRACT:
    {text}
    """)

    compare_prompt = ChatPromptTemplate.from_template("""
    Compare both contracts.

    Return JSON:
    {{
      "modified_clauses": [
        {{
          "clause_id": "...",
          "original_text": "...",
          "amended_text": "...",
          "change_type": "added|removed|modified",
          "legal_impact": "low|medium|high"
        }}
      ],
      "affected_topics": ["..."],
      "summary": "..."
    }}

    ORIGINAL:
    {original}

    AMENDED:
    {amended}
    """)

    validator_prompt = ChatPromptTemplate.from_template("""
You are a pedantic legal validator AI.

Tasks:
1. Ensure JSON is valid
2. Fix schema issues
3. Count modified_clauses
4. Add "total_changes" at the top

Return STRICT JSON:
{{
  "total_changes": int,
  "modified_clauses": [...],
  "affected_topics": [...],
  "summary": "..."
}}

INPUT:
{input_json}
""")

    structure_chain = structure_prompt | llm
    compare_chain = compare_prompt | llm
    validator_chain = validator_prompt | llm

    def structure(text):
        res = structure_chain.invoke({"text": text})
        track_tokens(res)
        return res.content

    def compare(o, a):
        res = compare_chain.invoke({"original": o, "amended": a})
        track_tokens(res)
        return res.content

    def validate(diff):
        res = validator_chain.invoke({"input_json": diff})
        track_tokens(res)
        return res.content

    return structure, compare, validate

# =========================
# UI
# =========================
def main():
    st.title("📄 LegalMove AI Comparator PRO")

    original = st.file_uploader("Original Contract", type=["pdf","png","jpg","jpeg"])
    amended = st.file_uploader("Amendment", type=["pdf","png","jpg","jpeg"])

    # ANALYZE BUTTON
    if st.button("Analyze"):
        if not original or not amended:
            st.error("Upload both files")
        else:
            with st.spinner("Processing..."):

                o_text = extract_text(original)
                a_text = extract_text(amended)

                structure, compare, validate = build_agents()

                o_struct = structure(o_text)
                a_struct = structure(a_text)

                raw_diff = compare(o_struct, a_struct)
                validated_diff = validate(raw_diff)

                try:
                    result = ContractDiff.model_validate_json(validated_diff)
                    result_dict = result.model_dump()

                    metrics = {
                        "openai": estimate_cost(),
                        "langfuse": {
                            "note": "Tracked via Langfuse dashboard"
                        }
                    }

                    json_str = json.dumps({
                        "result": result_dict,
                        "metrics": metrics
                    }, indent=2)

                    pdf_buffer = generate_pdf(result_dict, metrics)

                    # SAVE STATE
                    st.session_state.analysis_done = True
                    st.session_state.result = result_dict
                    st.session_state.metrics = metrics
                    st.session_state.json_str = json_str
                    st.session_state.pdf_buffer = pdf_buffer

                    log("final", {
                        "result": result_dict,
                        "metrics": metrics
                    })

                except ValidationError as e:
                    st.error("Validation failed")
                    st.text(validated_diff)
                    st.text(str(e))

    # =========================
    # DISPLAY RESULTS (PERSISTENT)
    # =========================
    if st.session_state.analysis_done:

        st.success("Analysis complete")

        st.subheader("📄 Contract Differences")
        st.json(st.session_state.result)

        st.subheader("📊 Metrics")
        st.json(st.session_state.metrics)

        # DOWNLOADS
        st.download_button(
            label="⬇️ Download JSON",
            data=st.session_state.json_str,
            file_name="contract_analysis.json",
            mime="application/json"
        )

        st.download_button(
            label="⬇️ Download PDF",
            data=st.session_state.pdf_buffer,
            file_name="contract_analysis.pdf",
            mime="application/pdf"
        )

        # RESET BUTTON
        if st.button("🔄 Reset"):
            st.session_state.clear()
            st.rerun()

# =========================
# RUN
# =========================
if __name__ == "__main__":
    main()