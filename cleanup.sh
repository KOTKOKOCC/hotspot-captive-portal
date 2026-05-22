#!/bin/bash

find . -name "__pycache__" -type d -prune -exec rm -rf {} +
find . -name "*.pyc" -delete
find . -name ".DS_Store" -delete
find . -name "*.bak" -delete
find . -name "*.save" -delete
find . -name "*.save.*" -delete

echo "Cleanup complete"
