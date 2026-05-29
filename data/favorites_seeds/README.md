# Favorites Seeds

Each JSON file here is a single HP favorite entry (matching the shape of
items in ).  These are committed to git so
favorites that were hand-built outside the UI (e.g., the ACY Airshow
profile rebuilt from the legacy gen-2 rtl-airband .conf) survive a
fresh deploy or a state reset.

 itself is gitignored because it carries runtime user
state — these seeds are the durable, sharable extract of what we want
to be reapplied to it.

Apply manually with:
\