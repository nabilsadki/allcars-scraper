#!/usr/bin/env python3
"""Point d'entree de l'outil de consultation : python3 /app/bin/query.py ads --help"""
import sys
sys.path.insert(0, "/app/src")
from avito.query import main
sys.exit(main())
