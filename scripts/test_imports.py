#!/usr/bin/env python3
import sys
sys.path.insert(0, ".")
try:
    from src.server import TranscriptionServer
    print("server import OK")
    from src.worker import TranscriptionWorker
    print("worker import OK")
    from src.enrichment import MetadataEnrichmentPipeline
    print("enrichment import OK")
    from src.transcript_index import TranscriptIndex
    print("transcript_index import OK")
    from src.sidecar import TranscriptSidecar
    print("sidecar import OK")
    from src.search_service import TranscriptSearchService
    print("search_service import OK")
    print("\nAll imports successful!")
except Exception as e:
    print(f"IMPORT ERROR: {e}")
    import traceback
    traceback.print_exc()
