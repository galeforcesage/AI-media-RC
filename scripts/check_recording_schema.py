#!/usr/bin/env python3
import requests, json
r = requests.get("http://localhost:8089/dvr/files")
data = r.json()
if data:
    rec = data[0]
    print("Duration:", rec.get("Duration"))
    airing = rec.get("Airing", {})
    print("Airing keys:", json.dumps({k: type(v).__name__ for k, v in airing.items()}, indent=2))
    print("Title:", airing.get("Title"))
    print("EpisodeTitle:", airing.get("EpisodeTitle"))
    print("SeriesID:", airing.get("SeriesID"))
    print("ProgramID:", airing.get("ProgramID"))
    print("Source:", airing.get("Source"))
