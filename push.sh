#!/bin/bash
# push.sh
# Quick script to commit and push all changes to GitHub.
# Usage: ./push.sh "your commit message"
# Example: ./push.sh "Updated middleware with persistent spool IDs"

# Use provided commit message, or fall back to a default
MESSAGE=${1:-"Update from Mac"}

git add .
git commit -m "$MESSAGE"
git push

echo "Done! Changes pushed to GitHub."
