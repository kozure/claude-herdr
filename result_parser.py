"""result_parser.py — parse Claude interactive terminal text into structured results.

Claude's interactive mode outputs terminal text with UI markers:
  ⏺ = assistant message prefix
  ❯ = user prompt prefix
  ✻ = status line prefix

Strategy: find the last ❯ prompt that has a ⏺ response after it.
Extract the ⏺ block between that prompt and the next ❯ (or end of text).
Fallback: if no ⏺ marker found after any ❯, return raw text + parse_warning.
"""
import re

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return _ANSI_RE.sub('', text)


def parse_result(raw_terminal: str) -> dict:
    """Parse Claude interactive terminal text into a structured result.

    Returns:
        {
            "text": str,          # extracted assistant response (or raw if fallback)
            "raw_terminal": str,  # original input preserved
            "model_info": str|None,  # model info if found, else None
            "parse_warning": str|None,  # None if successful, "markers_not_found" if fallback
        }
    """
    result = {
        "text": "",
        "raw_terminal": raw_terminal,
        "model_info": None,
        "parse_warning": None,
    }

    if not raw_terminal:
        result["parse_warning"] = "markers_not_found"
        return result

    clean = _strip_ansi(raw_terminal)

    # Find all ❯ prompt positions
    prompt_positions = [m.start() for m in re.finditer(r'❯', clean)]

    if not prompt_positions:
        # No prompt markers — fallback with raw text
        result["text"] = clean.strip()
        result["parse_warning"] = "markers_not_found"
        return result

    # Walk prompts in reverse to find the last one with a ⏺ response after it
    for i in range(len(prompt_positions) - 1, -1, -1):
        prompt_pos = prompt_positions[i]
        # Text between this prompt and the next (or end)
        if i + 1 < len(prompt_positions):
            segment = clean[prompt_pos:prompt_positions[i + 1]]
        else:
            segment = clean[prompt_pos:]

        block_match = re.search(r'⏺', segment)
        if block_match:
            # Extract from ⏺ to end of segment
            block_text = segment[block_match.end():]

            # Remove ✻ status lines
            lines = block_text.split('\n')
            response_lines = [line for line in lines if not line.strip().startswith('✻')]
            block_text = '\n'.join(response_lines).strip()

            if block_text:
                result["text"] = block_text

                # Try to extract model info from status lines
                model_match = re.search(r'model[:\s]+([^\s,]+)', raw_terminal, re.IGNORECASE)
                if model_match:
                    result["model_info"] = model_match.group(1)

                return result

    # No ❯ with a ⏺ response found — try finding any ⏺ in the full text
    block_match = re.search(r'⏺', clean)
    if block_match:
        block_text = clean[block_match.end():]
        # Cut at next ❯ if present
        next_prompt = block_text.find('❯')
        if next_prompt != -1:
            block_text = block_text[:next_prompt]
        lines = block_text.split('\n')
        response_lines = [line for line in lines if not line.strip().startswith('✻')]
        block_text = '\n'.join(response_lines).strip()
        if block_text:
            result["text"] = block_text
            return result

    # Fallback: return raw text with warning
    result["text"] = clean.strip()
    result["parse_warning"] = "markers_not_found"
    return result