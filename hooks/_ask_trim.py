# First sentence of an AskUserQuestion, capped at 80 chars. argv[1] = question.

def main():
    import re
    import sys
    q = sys.argv[1]
    # Split on sentence-ending punctuation followed by whitespace or end-of-string.
    parts = re.split(r'(?<=[.?!])\s+', q.strip())
    first_sentence = parts[0] if parts else q
    # Take the shorter of: first sentence, or first 80 chars (with ellipsis if cut).
    if len(first_sentence) <= 80:
        print(first_sentence)
    else:
        print(first_sentence[:77].rstrip() + '...')


if __name__ == "__main__":
    main()
