"""Provider-edge modules.

Each parser here is a pure adapter that normalizes one provider response
into the capacity contract. Provider-specific parsing lives only at this
edge. The Z.ai module additionally ships its production acquisition shell
(``zai_acquisition``); no other provider has live acquisition yet.
"""
