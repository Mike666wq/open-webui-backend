# Notes editor Markdown (P-005, S4)

The note editor saves three views of one document: TipTap JSON for live editing, HTML for display, and Markdown (`data.content.md`) for the **Export MD** action and P-004's **Save to knowledge base** snapshot. The snapshot reads the saved Markdown; existing snapshots do not update until the user saves again. The conversion preserves headings H1–H6, ordered lists with starting numbers, bullet and task lists, nested quotes, fenced code with language, tables and alignment, rules, links, images, strike, highlight (`==text==`), and superscript/subscript (`<sup>`/`<sub>`). Images using `data://` URLs depend on the note's attached files and may not resolve in a standalone Markdown export.

Enter starts a paragraph, and in a list it starts another item at the same level. Shift+Enter inserts a soft line break. In lists, Tab indents and Shift+Tab removes indentation; inside quotes, Enter continues the quote until its empty line exits. The toolbar supplies quote, 3×3 table with row/column actions, rule, highlight, super/subscript, and list indentation. The note shortcuts are Mod+Shift+B for quote, Mod+Shift+H for rule, Mod+Shift+M for highlight, Mod+Shift+Up/Down for super/subscript (Mod means Command on macOS and Control elsewhere). Existing chat input keyboard behavior is unchanged.

This is stage 1: editing and Markdown conversion. Stage 2 is the outline navigation and level changes, source/WYSIWYG switching, HTML paste cleaning, and local image handling. Stage 3 is internal links and `[[note]]` links, knowledge search from the P-004 mapping, and PDF/DOCX export. Stages 2 and 3 are future work.

Automated assertions cover conversion; browser editing, mobile rendering, and actual knowledge ingestion require manual acceptance after CI.
