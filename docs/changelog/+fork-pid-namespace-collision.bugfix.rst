A child with its parent's numeric PID in a different Linux PID namespace now preserves the parent's file and soft
reader/writer locks. Finalizing an inherited SQLite lock connection in the child leaves the parent's private database
link in place.
