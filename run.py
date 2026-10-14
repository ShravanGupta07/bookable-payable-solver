"""run.py — Single-command CLI entrypoint for the Bookable Payable Solver with Dynamic .env Support, Resume & Robust API Fallbacks."""
from __future__ import annotations
import argparse
import base64
import json
import os
import re
import sys
import time
from typing import Any, List

from dotenv import load_dotenv
import pymupdf as fitz  # PyMuPDF

from solver.agent_loop import solve_document
from solver.master_index import MasterDataIndex

# Automatically load environment variables from local .env file
load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))


def extract_pdf_evidence(pdf_path: str, max_image_pages: int = 15) -> tuple[str, List[bytes]]:
    doc = fitz.open(pdf_path)
    text_blocks: List[str] = []
    page_pngs: List[bytes] = []

    for page_idx, page in enumerate(doc):
        page_text = page.get_text("text").strip()
        text_blocks.append(f"--- PAGE {page_idx + 1} OF {len(doc)} ---\n{page_text or '[SCANNED IMAGE PAGE]'}")
        if page_idx < max_image_pages:
            pix = page.get_pixmap(dpi=100)
            page_pngs.append(pix.tobytes("jpeg", jpg_quality=85))

    doc.close()
    return "\n\n".join(text_blocks), page_pngs


def _parse_json_response(raw: str) -> dict[str, Any]:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", s, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def make_llm_caller(page_pngs: List[bytes]):
    """Build a multimodal LLM caller with robust cascading fallbacks:
    1. Google Gemini (GEMINI_API_KEY)
    2. Groq (GROQ_API_KEY)
    3. OpenRouter (OPENROUTER_API_KEY)
    4. OpenAI / Direct (OPENAI_API_KEY)
    """
    def call_with_fallbacks(messages: List[dict[str, str]], model_name: str) -> dict[str, Any]:
        gemini_key = os.environ.get("GEMINI_API_KEY")
        groq_key = os.environ.get("GROQ_API_KEY")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY")
        openai_key = os.environ.get("OPENAI_API_KEY")
        
        last_exc = None

        # 1. Try Google Gemini SDK
        if gemini_key:
            # Map legacy/deprecated models to active versions
            target_models = []
            if "gemini" in model_name and "2.5" not in model_name and "2.0" not in model_name:
                target_models.append(model_name)
            target_models.extend(["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-flash-latest"])

            for g_model in target_models:
                try:
                    from google import genai
                    from google.genai import types
                    client = genai.Client(api_key=gemini_key)
                    
                    system_instruction = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
                    contents: list[Any] = []
                    for turn_idx, m in enumerate(messages[1:]):
                        role = "user" if m["role"] == "user" else "model"
                        parts: list[Any] = []
                        if turn_idx == 0 and role == "user" and page_pngs:
                            for png in page_pngs:
                                parts.append(types.Part.from_bytes(data=png, mime_type="image/jpeg"))
                        parts.append(types.Part.from_text(text=m["content"]))
                        contents.append(types.Content(role=role, parts=parts))

                    resp = client.models.generate_content(
                        model=g_model,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=0.0,
                            response_mime_type="application/json",
                        ),
                    )
                    return _parse_json_response(resp.text)
                except Exception as e:
                    last_exc = e
                    continue

        # 2. Try Groq Fallback (fast & active)
        if groq_key:
            try:
                from openai import OpenAI
                groq_client = OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1")

                formatted_msgs: list[dict[str, Any]] = []
                for idx, m in enumerate(messages):
                    if idx == 1 and m["role"] == "user" and page_pngs:
                        content_parts: list[dict[str, Any]] = [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(png).decode('ascii')}"},
                            }
                            for png in page_pngs
                        ]
                        content_parts.append({"type": "text", "text": m["content"]})
                        formatted_msgs.append({"role": "user", "content": content_parts})
                    else:
                        formatted_msgs.append({"role": m["role"], "content": m["content"]})

                # Use qwen3.8-27b when images are included, or gpt-oss-120b for text-only
                groq_model = "qwen/qwen3.8-27b" if page_pngs else "openai/gpt-oss-120b"
                resp = groq_client.chat.completions.create(
                    model=groq_model,
                    messages=formatted_msgs,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                return _parse_json_response(resp.choices[0].message.content or "{}")
            except Exception as e:
                last_exc = e

        # 3. Try OpenRouter Fallback
        if openrouter_key:
            try:
                from openai import OpenAI
                or_client = OpenAI(api_key=openrouter_key, base_url="https://openrouter.ai/api/v1")
                
                formatted_msgs: list[dict[str, Any]] = []
                for idx, m in enumerate(messages):
                    if idx == 1 and m["role"] == "user" and page_pngs:
                        content_parts: list[dict[str, Any]] = [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(png).decode('ascii')}"},
                            }
                            for png in page_pngs
                        ]
                        content_parts.append({"type": "text", "text": m["content"]})
                        formatted_msgs.append({"role": "user", "content": content_parts})
                    else:
                        formatted_msgs.append({"role": m["role"], "content": m["content"]})

                or_models = [
                    "google/gemini-2.5-flash",
                    "google/gemini-2.0-flash-lite-preview-02-05:free",
                    "meta-llama/llama-3.3-70b-instruct:free",
                ]
                for or_m in or_models:
                    try:
                        resp = or_client.chat.completions.create(
                            model=or_m,
                            messages=formatted_msgs,
                            temperature=0.0,
                            response_format={"type": "json_object"},
                        )
                        return _parse_json_response(resp.choices[0].message.content or "{}")
                    except Exception as or_err:
                        last_exc = or_err
                        continue
            except Exception as e:
                last_exc = e

        # 4. Try OpenAI / Direct Fallback
        if openai_key:
            try:
                from openai import OpenAI
                o_client = OpenAI(api_key=openai_key)

                formatted_msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
                resp = o_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=formatted_msgs,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                return _parse_json_response(resp.choices[0].message.content or "{}")
            except Exception as e:
                last_exc = e

        raise RuntimeError(f"All LLM callers failed or no valid API key found in .env. Last error: {last_exc}")

    return call_with_fallbacks


def process_one_pdf(
    pdf_file: str,
    docs_dir: str,
    out_dir: str,
    master_idx: MasterDataIndex,
    fast_model: str,
    strong_model: str,
) -> tuple[str, bool]:
    doc_id = os.path.splitext(pdf_file)[0]
    out_path = os.path.join(out_dir, f"{doc_id}.json")

    # Resume feature: skip if output already exists and is non-empty
    if os.path.exists(out_path) and os.path.getsize(out_path) > 10:
        return doc_id, True  # Skipped

    pdf_path = os.path.join(docs_dir, pdf_file)
    extracted_text, page_pngs = extract_pdf_evidence(pdf_path)
    llm_caller = make_llm_caller(page_pngs)

    result = solve_document(
        doc_id=doc_id,
        extracted_text=extracted_text,
        master_idx=master_idx,
        llm_call=llm_caller,
        fast_model=fast_model,
        strong_model=strong_model,
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    return doc_id, False  # Newly processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Bookable Payable Agentic Solver")
    parser.add_argument("documents_dir", nargs="?", default=os.path.join(HERE, "documents"))
    parser.add_argument("output_dir", nargs="?", default=os.path.join(HERE, "output"))
    parser.add_argument("--master-dir", default=os.path.join(HERE, "master_data"))
    parser.add_argument("--fast-model", default=os.environ.get("FAST_MODEL", "gemini-3.5-flash-lite"))
    parser.add_argument("--strong-model", default=os.environ.get("STRONG_MODEL", "gemini-3.5-flash"))
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    master_idx = MasterDataIndex(args.master_dir)

    pdf_files = sorted(f for f in os.listdir(args.documents_dir) if f.lower().endswith(".pdf"))
    if not pdf_files:
        print(f"No PDF files found in {args.documents_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Solving {len(pdf_files)} documents from '{args.documents_dir}' -> '{args.output_dir}' (.env enabled)...")
    
    for pdf in pdf_files:
        try:
            doc_id, skipped = process_one_pdf(
                pdf,
                args.documents_dir,
                args.output_dir,
                master_idx,
                args.fast_model,
                args.strong_model,
            )
            if skipped:
                print(f"  [SKIPPED - ALREADY EXISTS] {doc_id}.json")
            else:
                print(f"  [OK] {doc_id}.json")
        except Exception as exc:
            print(f"  [ERR] {pdf}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()