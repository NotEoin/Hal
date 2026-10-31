## Find

1. Pick the most specific identifier in the task: an error message, a function or class name, a parameter, or a literal string. Copy it exactly.
2. Search for it with the grep tool. Use the glob tool to find files by name.
3. Read only the lines around the hits with the read tool, giving start_line and end_line. Never read a whole large file.
4. Choose the file that owns the behaviour. Prefer source code over examples, docs and tests.

## Edit

- Change the code with the edit tool. Copy old_string exactly from the read output, without the line numbers, and include enough lines to make it unique. If an edit fails, read the lines again and retry once. For several changes in one file, use patch with SEARCH/REPLACE blocks.
- Fix the cause with the smallest change. Keep the existing style, names and public API.
- Change tests only when the task asks for it.
- Put scratch files in /scratch, never in the project.
