#!/usr/bin/env python3

import argparse
import asyncio
import collections
import dataclasses
import json
import logging
import os
import pathlib
import urllib.parse
from typing import AbstractSet, Dict, List, Set

from plants.committer import Committer
from plants.environment import Environment
from plants.external import allow_external_calls
from spotify import Spotify, SpotifyPlaylist

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger: logging.Logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class GitHubPlaylist:
    # Playlist ID of the scraped playlist
    playlist_id: str
    name: str
    description: str
    track_ids: AbstractSet[str]


@dataclasses.dataclass(frozen=True)
class Playlist:
    scraped_playlist_id: str
    published_playlist_ids: List[str]


@dataclasses.dataclass(frozen=True)
class Playlists:
    playlists: List[Playlist]

    def to_json(self) -> str:
        return json.dumps(
            dataclasses.asdict(self),
            indent=2,
            sort_keys=True,
        )


class GitHub:
    @classmethod
    async def get_playlists(cls, playlists_dir: pathlib.Path) -> List[GitHubPlaylist]:
        logger.info(f"Reading playlists from {playlists_dir}")

        json_files: List[pathlib.Path] = []
        for path in (playlists_dir / "cumulative").iterdir():
            if str(path).endswith(".json"):
                json_files.append(path)

        github_playlists: List[GitHubPlaylist] = []
        for path in json_files:
            with open(path, "r") as f:
                playlist = json.load(f)
            track_ids: Set[str] = set()
            for track in playlist["tracks"]:
                track_id = track["url"].split("/")[-1]
                track_ids.add(track_id)
            github_playlists.append(
                GitHubPlaylist(
                    playlist_id=playlist["url"].split("/")[-1],
                    name=playlist["name"] + " (Cumulative)",
                    description=playlist["description"],
                    track_ids=track_ids,
                )
            )

        return github_playlists


async def publish(playlists_dir: pathlib.Path, prod: bool) -> None:

    # Check nonempty to fail fast
    client_id = Environment.get_env("SPOTIFY_CLIENT_ID")
    client_secret = Environment.get_env("SPOTIFY_CLIENT_SECRET")
    refresh_token = Environment.get_env("SPOTIFY_REFRESH_TOKEN")
    assert client_id and client_secret and refresh_token

    # Initialize Spotify client
    access_token = await Spotify.get_user_access_token(
        client_id, client_secret, refresh_token
    )
    spotify = Spotify(access_token)
    try:
        await publish_impl(spotify, playlists_dir, prod)
    finally:
        await spotify.shutdown()
    if prod:
        Committer.commit_and_push_if_github_actions()


async def publish_impl(
    spotify: Spotify, playlists_dir: pathlib.Path, prod: bool
) -> None:
    # Always read all GitHub playlists from local storage
    playlists_in_github = await GitHub.get_playlists(playlists_dir)

    # When testing, only fetch one playlist to avoid rate limits
    if prod:
        playlists_in_spotify = await spotify.get_playlists()
    else:
        playlists_in_spotify = await spotify.get_playlists(limit=1)
        # Find the corresponding GitHub playlist
        name = playlists_in_spotify[0].name
        while playlists_in_github[0].name != name:
            playlists_in_github = playlists_in_github[1:]
        playlists_in_github = playlists_in_github[:1]

    # Key playlists by name for quick retrieval
    github_playlists = {p.name: p for p in playlists_in_github}
    spotify_playlists = {p.name: p for p in playlists_in_spotify}

    playlists_to_create = set(github_playlists) - set(spotify_playlists)
    playlists_to_delete = set(spotify_playlists) - set(github_playlists)

    # Create missing playlists
    for name in sorted(playlists_to_create):
        logger.info(f"Creating playlist: {name}")
        if prod:
            playlist_id = await spotify.create_playlist(name)
        else:
            # When testing, just use a fake playlist ID
            playlist_id = f"playlist_id:{name}"
        spotify_playlists[name] = SpotifyPlaylist(
            playlist_id=playlist_id,
            name=name,
            description="",
            track_ids=set(),
        )

    # Update existing playlists
    for name, github_playlist in github_playlists.items():
        github_track_ids = github_playlist.track_ids

        spotify_playlist = spotify_playlists[name]
        playlist_id = spotify_playlist.playlist_id
        spotify_track_ids = spotify_playlist.track_ids

        tracks_to_add = list(github_track_ids - spotify_track_ids)
        tracks_to_remove = list(spotify_track_ids - github_track_ids)

        if tracks_to_add:
            logger.info(f"Adding tracks to playlist: {name}")
            if prod:
                await spotify.add_items(playlist_id, tracks_to_add)

        if tracks_to_remove:
            logger.info(f"Removing tracks from playlist: {name}")
            if prod:
                await spotify.remove_items(playlist_id, tracks_to_remove)

    # Remove extra playlists
    for name in playlists_to_delete:
        playlist_id = spotify_playlists[name].playlist_id
        logger.info(f"Unsubscribing from playlist: {name}")
        if prod:
            await spotify.unsubscribe_from_playlist(playlist_id)

    # Dump JSON
    scraped_to_published: Dict[str, List[str]] = collections.defaultdict(list)
    for name, github_playlist in github_playlists.items():
        scraped_to_published[github_playlist.playlist_id].append(
            spotify_playlists[name].playlist_id
        )
    playlists = Playlists(
        playlists=[
            Playlist(
                scraped_playlist_id=scraped_id,
                published_playlist_ids=published_ids,
            )
            for scraped_id, published_ids in scraped_to_published.items()
        ]
    )
    repo_dir = Environment.get_repo_root()
    json_path = repo_dir / "playlists.json"
    with open(json_path, "w") as f:
        f.write(playlists.to_json())


async def login() -> None:
    # Login OAuth flow.
    #
    # 1. Opens the authorize url in the default browser (on Linux).
    # 2. Sets up an HTTP server on port 8000 to listen for the callback.
    # 3. Requests a refresh token for the user and prints it.

    # Build the target URL
    client_id = Environment.get_env("SPOTIFY_CLIENT_ID")
    client_secret = Environment.get_env("SPOTIFY_CLIENT_SECRET")
    assert client_id and client_secret
    query_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": Spotify.REDIRECT_URI,
        "scope": "playlist-modify-public",
    }
    target_url = "https://accounts.spotify.com/authorize?{}".format(
        urllib.parse.urlencode(query_params)
    )

    # Print and try to open the URL in the default browser.
    print("Opening the following URL in a browser (at least trying to):")
    print(target_url)
    os.system("xdg-open '{}'".format(target_url))

    # Set up a temporary HTTP server and listen for the callback
    import socketserver
    from http import HTTPStatus
    from http.server import BaseHTTPRequestHandler

    authorization_code: str = ""

    class RequestHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal authorization_code
            request_url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(request_url.query)
            authorization_code = q["code"][0]

            self.send_response(HTTPStatus.OK)
            self.end_headers()
            self.wfile.write(b"OK!")

    PORT = 8000
    httpd = socketserver.TCPServer(("", PORT), RequestHandler)
    httpd.handle_request()
    httpd.server_close()

    # Request a refresh token for given the authorization code
    refresh_token = await Spotify.get_user_refresh_token(
        client_id=client_id,
        client_secret=client_secret,
        authorization_code=authorization_code,
    )

    print("Refresh token, store this somewhere safe and use for the export feature:")
    print(refresh_token)


def argparse_directory(arg: str) -> pathlib.Path:
    path = pathlib.Path(arg)
    if path.is_dir():
        return path
    raise argparse.ArgumentTypeError(f"{arg} is not a valid directory")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Publish playlists to Spotify")
    subparsers = parser.add_subparsers(dest="action", required=True)

    publish_parser = subparsers.add_parser(
        "publish",
        help="Fetch and publish playlists and tracks",
    )
    publish_parser.add_argument(
        "--playlists",
        required=True,
        type=argparse_directory,
        help="Path to the local playlists directory",
    )
    publish_parser.add_argument(
        "--prod",
        action="store_true",
        help="Actually publish changes to Spotify",
    )
    publish_parser.set_defaults(func=lambda args: publish(args.playlists, args.prod))

    login_parser = subparsers.add_parser(
        "login",
        help="Obtain a refresh token through the OAuth flow",
    )
    login_parser.set_defaults(func=lambda args: login())

    args = parser.parse_args()
    await args.func(args)


if __name__ == "__main__":
    allow_external_calls()
    asyncio.run(main())
