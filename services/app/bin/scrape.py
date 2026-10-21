#!/usr/bin/env python3
"""Point d'entree du scraper : python3 /app/bin/scrape.py list --help"""
import sys
sys.path.insert(0, "/app/src")
from scraper.cli import main
sys.exit(main())
