from functools import lru_cache

import tiktoken
from tiktoken import Encoding


TOKENIZER_NAME = "o200k_base"


@lru_cache(maxsize=1)
def _tokenizer() -> Encoding:
    return tiktoken.get_encoding(TOKENIZER_NAME)


def text_counts(text: str) -> tuple[int, int]:
    """Return Unicode character and o200k_base token counts for text."""
    return len(text), len(_tokenizer().encode(text, disallowed_special=()))
