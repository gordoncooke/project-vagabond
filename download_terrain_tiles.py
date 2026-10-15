import argparse
import math
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download terrain tiles for a longitude/latitude bounding box."
    )
    parser.add_argument("west", type=float, help="Western longitude")
    parser.add_argument("south", type=float, help="Southern latitude")
    parser.add_argument("east", type=float, help="Eastern longitude")
    parser.add_argument("north", type=float, help="Northern latitude")
    return parser.parse_args()


def longitude_to_x(longitude, zoom):
    return int((longitude + 180) / 360 * (2 ** zoom))


def latitude_to_y(latitude, zoom):
    radians = math.radians(latitude)
    return int((1 - math.asinh(math.tan(radians)) / math.pi) / 2 * (2 ** zoom))


def main():
    args = parse_args()
    west, south, east, north = args.west, args.south, args.east, args.north
    output_root = "/srv/library/maps/data/terrain"

    tiles = []
    for zoom in range(13):
        for x in range(longitude_to_x(west, zoom), longitude_to_x(east, zoom) + 1):
            for y in range(latitude_to_y(north, zoom), latitude_to_y(south, zoom) + 1):
                tiles.append((zoom, x, y))

    def download(tile):
        zoom, x, y = tile
        directory = os.path.join(output_root, str(zoom), str(x))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{y}.webp")
        if os.path.isfile(path) and os.path.getsize(path):
            return
        url = f"https://tiles.mapterhorn.com/{zoom}/{x}/{y}.webp"
        for attempt in range(3):
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Offline-Emergency-Library/1.0"},
                )
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = response.read()
                with open(path, "wb") as output:
                    output.write(data)
                return
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(download, tile) for tile in tiles]
        for future in as_completed(futures):
            future.result()


if __name__ == "__main__":
    main()
