# Code search

The `lodestone` MCP server indexes the definitions in your checkout, including your edits.
Its tools find code by what it does, which a text search cannot when you do not know the name.

| Situation | Tool | Input |
| ---- | ---- | ---- |
| Before writing a helper, type, trait or macro | `semantic_code_search` | what it must do |
| Locating described behaviour | `semantic_code_search` | the behaviour |
| Reviewing or refactoring | `similar_code_search`, once | each added or moved function, one per entry |

- `semantic_code_search` takes `query`, one plain sentence, and `example_identifiers`, three
  to five guessed names in this codebase's style. Wrong guesses still help. Rephrase around
  the capability and search again when the answer is missing.
- Results are `path:line:col  lines start-end  kind name(signature) — doc`. Read the likely
  candidates, and reuse what fits instead of writing another version.
- Use a text search for exact strings: an error message, a known identifier, a config key.
- When the tools are missing, fail or report a stale index, continue with the shell and the
  source. Empty results do not show that no such code exists.
