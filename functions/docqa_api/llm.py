"""Everything that talks to the GLM-4.7 Flash model on Catalyst QuickML."""
import json
import logging
import os
import re

from zcatalyst_sdk._constants import CatalystService, CredentialUser, RequestMethod
from zcatalyst_sdk._http_client import AuthorizedHttpClient

# The model's address inside your project, taken from the endpoint page:
# https://console.catalyst.zoho.in/quickml/v1/project/<project id>/genai/endpoints/glm-flash-47/generate
# The SDK adds the "https://console.../quickml/v1/project/<id>" part itself.
GENERATE_PATH = "/genai/endpoints/glm-flash-47/generate"
CATALYST_ORG = "60091697131"   # listed as a required header on the endpoint page
# The docqa_llm endpoint's key, set as an environment variable on the function
# (catalyst-config.json -> env_variables) rather than written into the code.
ENDPOINT_KEY = os.environ.get("QUICKML_ENDPOINT_KEY", "")

logger = logging.getLogger()


class LLMError(Exception):
    pass


def _post(app, prompt):
    client = AuthorizedHttpClient(app)
    return client.request(
        method=RequestMethod.POST,
        path=GENERATE_PATH,
        json={"prompt": prompt},
        user=CredentialUser.ADMIN,
        catalyst_service=CatalystService.QUICK_ML,
        headers={"CATALYST-ORG": CATALYST_ORG, "X-QUICKML-ENDPOINT-KEY": ENDPOINT_KEY},
    )


def generate(app, prompt):
    """Send one prompt to the model and return its reply as plain text."""
    try:
        resp = _post(app, prompt)  # the SDK raises by itself for any non-2xx reply
    except Exception as e:
        logger.exception("QuickML call failed")
        raise LLMError(f"AI service error: {str(e)[:300]}") from e

    try:
        payload = resp.response.json()
    except ValueError:  # reply was plain text, not JSON
        payload = resp.response.text

    text = _find_text(payload)
    if not text:
        logger.error("Unexpected QuickML response: %s", str(payload)[:1000])
        raise LLMError("Could not read the model's reply")
    return _strip_thinking(text).strip()


def _find_text(payload):
    """QuickML replies like {"data": [{"data": "<the answer>"}], "usage": {...}}."""
    try:
        return "".join(item["data"] for item in payload["data"])
    except (KeyError, TypeError):
        return None


def _strip_thinking(text):
    """Some models write private reasoning in <think>...</think> before answering."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)


def _parse_json(text):
    """Pull the first JSON object out of a reply that may have extra words around it."""
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _format_history(history):
    lines = []
    for msg in history:
        speaker = "User" if msg["role"] == "user" else "Assistant"
        lines.append(f"{speaker}: {msg['content'][:1000]}")
    return "\n".join(lines)


def rewrite_query(app, question, history):
    """Turn a question (plus the recent chat) into search keywords.

    "What about his salary there?" -> "salary compensation pay CTC Infosys"
    Falls back to the raw question if the model call fails.
    """
    if not history:
        context = "(no earlier messages)"
    else:
        context = _format_history(history[-4:])

    prompt = (
        "You turn a user's question into keywords for a keyword search over their documents.\n"
        "Use the conversation to resolve words like 'it', 'that', 'he', 'the second one'.\n"
        "Include the important words from the question plus 3-6 synonyms or closely related terms.\n"
        "Reply with ONLY the keywords on one line, separated by spaces. No explanation.\n\n"
        f"Conversation so far:\n{context}\n\n"
        f"Question: {question}\n\n"
        "Keywords:"
    )
    try:
        keywords = generate(app, prompt).splitlines()[0]
        return f"{question} {keywords}"
    except Exception:  # search still works on the plain question
        logger.exception("Query rewrite failed")
        return question


def answer_question(app, question, excerpts, history):
    """Ask the model to answer `question` using only `excerpts`.

    `excerpts` is a list of dicts with "file_name", "chunk_index" and "content".
    Each excerpt is numbered [1], [2], ... so the model can cite them.
    """
    if excerpts:
        blocks = [
            f"[{i}] (from {e['file_name']})\n{e['content']}"
            for i, e in enumerate(excerpts, start=1)
        ]
        excerpt_text = "\n\n".join(blocks)
    else:
        excerpt_text = "(no matching excerpts were found)"

    history_text = _format_history(history[-6:]) if history else "(none)"

    prompt = (
        "You are a document assistant. Answer the user's question using ONLY the numbered "
        "excerpts below from the user's own documents.\n"
        "Rules:\n"
        "- If the answer is not in the excerpts, say: \"I couldn't find that in your documents.\"\n"
        "- Do not use outside knowledge or guess.\n"
        "- Cite the excerpts you used with their numbers in square brackets, like [1] or [2][3].\n"
        "- Be clear and concise. Use short bullet points when listing several items.\n"
        "- Use the earlier conversation only to understand what the question refers to.\n\n"
        f"Earlier conversation:\n{history_text}\n\n"
        f"Excerpts:\n{excerpt_text}\n\n"
        f"Question: {question}\n\n"
        "Answer:"
    )
    return generate(app, prompt)


def summarize_document(app, file_name, text):
    """Return (summary, [3 suggested questions]) for a newly uploaded document.

    Only the start of the document is sent, to keep the call fast.
    """
    prompt = (
        "Read the start of this document and reply with ONLY a JSON object in this exact shape:\n"
        '{"summary": "<2-3 sentence summary>", '
        '"questions": ["<question 1>", "<question 2>", "<question 3>"]}\n'
        "The questions must be ones this document can answer.\n\n"
        f"Document name: {file_name}\n"
        f"Document text:\n{text[:6000]}"
    )
    try:
        data = _parse_json(generate(app, prompt)) or {}
    except Exception:
        logger.exception("Summary failed")
        data = {}

    summary = str(data.get("summary", "")).strip()[:2000]
    questions = [str(q).strip()[:300] for q in data.get("questions", []) if str(q).strip()][:3]
    return summary, questions
