"""Provider-edge modules.

Each parser here is a pure adapter that normalizes one provider response
into the capacity contract. Provider-specific parsing lives only at this
edge. The Z.ai (``zai``/``zai_acquisition``) and OpenAI Codex app-server
(``openai_codex``/``openai_codex_acquisition``) modules ship their
production acquisition shells; no other provider has live acquisition yet.
"""
