# Commit message measurement

This file states exactly how the Git hooks measure a commit message. It is
reference material for a refusal you have already met. It is not something you
must read before you commit.

`AGENTS.md` holds the rule itself: commit subjects use conventional-commits
prefixes and stay at or under 72 characters, body lines wrap at 72, and a line
is exempt for two reasons only, its text cannot be wrapped or Git wrote it.
Everything below says how a line is measured, and how each exemption is earned
and lost.

- A line ends at a line feed and nowhere else, because that is where Git ends
  one. A form feed, a vertical tab, U+0085, U+2028 and U+2029 are characters
  Git stores inside a line, so they do not shorten it and they do not divide
  it. A carriage return ends a line only as the first half of a CRLF ending.
- A blank line is a line where Git would see nothing: spaces, tabs, and the
  other whitespace Git itself recognizes. A non-breaking space, an ideographic
  space or an en quad is content. Git commits it and shows it, so a line of
  them is not the blank line under the subject, and they do not pad a line
  invisibly past the limit either.
- A line is measured exactly as it is written, trailing whitespace included,
  because `--cleanup=verbatim` stores it and the hooks are never told which
  cleanup mode Git will apply. Git does not even apply the mode at a fixed
  time: a message given with `-F` is cleaned before the hooks run, an edited
  message after. So a subject of 72 characters followed by 20 spaces is a
  subject of 92 characters. `git log` hides this, because it trims the subject
  it prints, but `git cat-file` shows the stored line in full. The cost is that
  an edited message padded past the limit with trailing whitespace is refused
  even though Git's default cleanup would have trimmed it to a legal length.
  The refusal names the trailing whitespace, and removing it is the fix.
- One carriage return directly before the line feed is part of how a CRLF file
  ends a line, so it is not counted. That is the only character the hooks
  remove, and the lenience stops there: a second carriage return, or a space
  next to it, is content and is measured. Under `--cleanup=verbatim` Git stores
  that one carriage return, so a 72-character line in a CRLF file is stored as
  73 and still accepted.
- Text that cannot be wrapped means a line with no break point at or before
  column 72. A break point is any whitespace, so a tab breaks a line exactly as
  a space does and a row of short words joined by tabs is ordinary prose that
  wraps. Only a non-breaking space is not a break point, because that is what
  the character means. A single long token such as a URL or a path, and a Git
  trailer whose value is one token, have no break point and are exempt.
- A break point needs text before it on the line to keep. Whitespace before the
  first word is indentation, and breaking there would leave an empty line above
  the same long line, so it is not a break point. One long token is therefore
  exempt whether you indent it or not, and prose is measured whether you indent
  it or not.
- Two markers let you declare text preformatted, which turns the column limit
  off for it: four leading spaces, and a closed fenced block. A fence closes
  only on the same character, on a run at least as long as the opener, and
  with nothing after it, so a fence that never closes exempts nothing. Use
  either one for pasted output, code or a table. Both are declarations you
  make, so do not use them to avoid wrapping prose.
- Text Git wrote means the template and the diff that a plain `git commit`
  puts in front of your editor. The hooks identify it by recording the message
  file before the editor opens, never by how a line looks. A comment character,
  a tab, a rename arrow, a scissors bar or a diff header that you type yourself
  carries no exemption and is measured like any other body line.
- The recording is all or nothing, and the comparison is byte for byte. Write
  above Git's text and leave it alone, and it is exempt. Delete part of it,
  edit one of its lines, or add a line below it, and none of the message is
  exempt any more, your text and Git's alike. Adding or removing trailing
  whitespace on one of Git's lines is such an edit, so an editor that trims
  trailing whitespace when it saves will rewrite Git's text and cost you the
  exemption. With `commit.verbose` the recording holds the diff, where trailing
  whitespace is common, so this is where you will meet it. Commit without the
  diff, with `git -c commit.verbose=false commit`, or turn the trimming off.
  Copying a line out of the diff Git showed you does not make that line Git's.
- Every other way of writing a message is measured in full: `-m`, `-F`,
  `git merge -m`, a merge or squash message, a reused message, a configured
  commit template, and a mailed patch. Git does not tell the hooks apart from
  the author in those cases, so nothing in them is excused.
- A subject Git generates, such as a merge, a revert or an autosquash marker,
  is exempt from the subject form alone. Its body follows the same rules as any
  other body.
