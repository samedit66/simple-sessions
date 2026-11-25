# Documentation conventions

1. Write English docstrings for the public Python API. Start with the outcome or guarantee visible to the caller. One sentence is enough when it conveys the contract.
2. Use Google-style `Args`, `Returns` and `Raises` sections where they clarify units, defaults, side effects or failure behavior. Let signatures carry type information.
3. Keep the practical quickstart in the English `README.md`. Update `docs/tutorial en.md` and `docs/tutorial ru.md` together. Document backend obligations on `SessionStore` so adapter authors can find the contract at the implementation boundary.
