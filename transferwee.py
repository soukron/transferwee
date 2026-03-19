#!/usr/bin/env python3

#
# Copyright (c) 2018-2023 Leonardo Taccari
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
# TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#


"""
Download/upload files via wetransfer.com

transferwee is a script/module to download/upload files via wetransfer.com.

It exposes `download' and `upload' subcommands, respectively used to download
files from a `we.tl' or `wetransfer.com/downloads' URLs and upload files that
will be shared via emails or link.
"""

import random
import string
import uuid
from typing import Any, Dict, List, Optional, Union
import binascii
import functools
import hashlib
import json
import logging
import os
import os.path
import re
import time
import urllib.parse

import requests

WETRANSFER_API_URL = "https://wetransfer.com/api/v4/transfers"
WETRANSFER_DOWNLOAD_URL = WETRANSFER_API_URL + "/{transfer_id}/download"
WETRANSFER_UPLOAD_URL = WETRANSFER_API_URL + "/{transfer_id}/passwordless"
WETRANSFER_VERIFY_URL = WETRANSFER_API_URL + "/{transfer_id}/verify"
WETRANSFER_FINALIZE_URL = WETRANSFER_API_URL + "/{transfer_id}/finalize"

WETRANSFER_EXPIRE_IN = 604800
WETRANSFER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:102.0) Gecko/20100101 Firefox/102.0"

WETRANSFER_AUTH0_URL = "https://auth.wetransfer.com/oauth/token"
WETRANSFER_AUTH0_CLIENT_ID = "dXWFQjiW1jxWCFG0hOVpqrk4h9vGeanc"
WETRANSFER_AUTH0_AUDIENCE = "aud://transfer-api-prod.wetransfer/"

WETRANSFER_OAUTH_CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "transferwee",
    "oauth_config.json",
)


logger = logging.getLogger(__name__)


def _load_oauth_config() -> Dict[str, str]:
    """Load OAuth config from disk, falling back to hardcoded defaults."""
    config = {
        "client_id": WETRANSFER_AUTH0_CLIENT_ID,
        "audience": WETRANSFER_AUTH0_AUDIENCE,
    }
    if os.path.exists(WETRANSFER_OAUTH_CONFIG):
        try:
            with open(WETRANSFER_OAUTH_CONFIG, "r") as f:
                stored = json.load(f)
            config.update(
                {k: v for k, v in stored.items() if k in config and v}
            )
            logger.debug(f"OAuth config loaded from {WETRANSFER_OAUTH_CONFIG}")
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(f"Could not read OAuth config: {e}")
    return config


def _save_oauth_config(client_id: str, audience: str) -> None:
    """Persist OAuth config overrides to disk."""
    config_dir = os.path.dirname(WETRANSFER_OAUTH_CONFIG)
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    payload = {"client_id": client_id, "audience": audience}
    with open(WETRANSFER_OAUTH_CONFIG, "w") as f:
        json.dump(payload, f, indent=2)
    os.chmod(WETRANSFER_OAUTH_CONFIG, 0o600)
    logger.debug(f"OAuth config saved to {WETRANSFER_OAUTH_CONFIG}")


def download_url(url: str) -> Optional[str]:
    """Given a wetransfer.com download URL download return the downloadable URL.

    The URL should be of the form `https://we.tl/' or
    `https://wetransfer.com/downloads/'. If it is a short URL (i.e. `we.tl')
    the redirect is followed in order to retrieve the corresponding
    `wetransfer.com/downloads/' URL.

    The following type of URLs are supported:
     - `https://we.tl/<short_url_id>`:
        received via link upload, via email to the sender and printed by
        `upload` action
     - `https://wetransfer.com/<transfer_id>/<security_hash>`:
        directly not shared in any ways but the short URLs actually redirect to
        them
     - `https://wetransfer.com/<transfer_id>/<recipient_id>/<security_hash>`:
        received via email by recipients when the files are shared via email
        upload

    Return the download URL (AKA `direct_link') as a str or None if the URL
    could not be parsed.
    """
    logger.debug(f"Getting download URL of {url}")
    # Follow the redirect if we have a short URL
    if url.startswith("https://we.tl/"):
        r = requests.head(
            url,
            allow_redirects=True,
            headers={"User-Agent": WETRANSFER_USER_AGENT},
        )
        logger.debug(f"Short URL {url} redirects to {r.url}")
        url = r.url

    recipient_id = None
    params = urllib.parse.urlparse(url).path.split("/")[2:]

    if len(params) == 2:
        transfer_id, security_hash = params
    elif len(params) == 3:
        transfer_id, recipient_id, security_hash = params
    else:
        return None

    logger.debug(f"Getting direct_link of {url}")
    j = {
        "intent": "entire_transfer",
        "security_hash": security_hash,
    }
    if recipient_id:
        j["recipient_id"] = recipient_id
    s = _prepare_session()
    if not s:
        raise ConnectionError("Could not prepare session")
    r = s.post(WETRANSFER_DOWNLOAD_URL.format(transfer_id=transfer_id), json=j)
    _close_session(s)

    j = r.json()
    return j.get("direct_link")


def _file_unquote(file: str) -> str:
    """Given a URL encoded file unquote it.

    All occurences of `\', `/' and `../' will be ignored to avoid possible
    directory traversals.
    """
    return (
        urllib.parse.unquote(file)
        .replace("../", "")
        .replace("/", "")
        .replace("\\", "")
    )


def download(url: str, file: str = "") -> None:
    """Given a `we.tl/' or `wetransfer.com/downloads/' download it.

    First a direct link is retrieved (via download_url()), the filename can be
    provided via the optional `file' argument. If not provided the filename
    will be extracted to it and it will be fetched and stored on the current
    working directory.
    """
    logger.debug(f"Downloading {url}")
    dl_url = download_url(url)
    if not dl_url:
        logger.error(f"Could not find direct link of {url}")
        return None
    if not file:
        file = _file_unquote(urllib.parse.urlparse(dl_url).path.split("/")[-1])

    logger.debug(f"Fetching {dl_url}")
    r = requests.get(
        dl_url, headers={"User-Agent": WETRANSFER_USER_AGENT}, stream=True
    )
    with open(file, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024):
            f.write(chunk)


def _file_name_and_size(file: str) -> Dict[str, Union[int, str]]:
    """Given a file, prepare the "item_type", "name" and "size" dictionary.

    Return a dictionary with "item_type", "name" and "size" keys.
    """
    filename = os.path.basename(file)
    filesize = os.path.getsize(file)

    return {"item_type": "file", "name": filename, "size": filesize}


def _prepare_session() -> Optional[requests.Session]:
    """Prepare a wetransfer.com session."""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": WETRANSFER_USER_AGENT,
            "x-requested-with": "XMLHttpRequest",
        }
    )
    return s


def _close_session(s: requests.Session) -> None:
    """Close a wetransfer.com session.

    Terminate wetransfer.com session.
    """
    s.close()


def generate_random_uuid():
    return str(uuid.uuid4())


def _prepare_email_upload(
    filenames: List[str],
    display_name: str,
    message: str,
    sender: str,
    recipients: List[str],
    session: requests.Session,
) -> Dict[Any, Any]:
    """Given a list of filenames, message a sender and recipients prepare for
    the email upload.

    Return the parsed JSON response.
    """
    lsid = generate_random_uuid()

    j = {
        "downloader_email_verification": "anonymous",
        "files": [_file_name_and_size(f) for f in filenames],
        "from": sender,
        "lsid": lsid,
        "display_name": display_name,
        "message": message,
        "recipients": recipients,
        "ui_language": "en",
    }

    r = session.post(WETRANSFER_API_URL, json=j)
    r.raise_for_status()

    return {"sender": sender, "id": r.json()["id"], "lsid": lsid}


def generate_client_id():
    """Generates a random client_id: 32 characters of letters and digits."""
    characters = string.ascii_letters + string.digits
    return "".join(random.choice(characters) for _ in range(32))


def _verify_email_upload(
    transfer_data: dict, session: requests.Session
) -> Dict[Any, Any]:
    """Given transfer_data, trigger passwordless login and verify via OTP.

    Return the parsed JSON response.
    """
    logger.debug("send code to sender email")
    c_id = generate_client_id()
    data = {"client_id": c_id, "email": transfer_data["sender"]}
    url = "https://wetransfer.com/adroit/api/v1/login/passwordless"
    response = requests.post(url, json=data)
    response.raise_for_status()

    code = input("Confirmation code sent to your email:")

    headers = {
        "user-agent": WETRANSFER_USER_AGENT,
        "x-csrf-token": "csrf-token",
    }

    logger.debug("get access_token")
    data = {
        "grant_type": "http://auth0.com/oauth/grant-type/passwordless/otp",
        "client_id": c_id,
        "otp": code,
        "realm": "email",
        "username": transfer_data["sender"],
    }
    url = "https://auth.wetransfer.com/oauth/token"
    response = requests.post(url, headers=headers, json=data)
    response.raise_for_status()
    tk = response.json()["access_token"]

    logger.debug("Confirm upload")
    headers = {
        "authorization": f"Bearer {tk}",
        "user-agent": WETRANSFER_USER_AGENT,
    }
    payload = {
        "expire_in": 259200,
        "lsid": transfer_data["lsid"],
        "segment_id": "grace_period",
    }
    response = requests.post(
        WETRANSFER_UPLOAD_URL.format(transfer_id=transfer_data["id"]),
        headers=headers,
        json=payload,
    )
    response.raise_for_status()

    return response.json()


WETRANSFER_AUTH_CACHE_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "transferwee",
)


def _auth_cache_path(email: str) -> str:
    """Return the cache file path for a given email."""
    email_hash = hashlib.sha256(email.lower().encode()).hexdigest()[:16]
    return os.path.join(WETRANSFER_AUTH_CACHE_DIR, f"auth_{email_hash}.json")


def _save_auth_cache(
    email: str,
    access_token: str,
    refresh_token: Optional[str],
) -> None:
    """Persist auth tokens to disk for later reuse."""
    if not refresh_token:
        return
    cache_file = _auth_cache_path(email)
    os.makedirs(WETRANSFER_AUTH_CACHE_DIR, mode=0o700, exist_ok=True)
    payload = {
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
    }
    with open(cache_file, "w") as f:
        json.dump(payload, f)
    os.chmod(cache_file, 0o600)
    logger.debug(f"Auth tokens cached to {cache_file}")


def _load_cached_auth(email: str) -> Optional[str]:
    """Try to obtain a fresh access_token using a cached refresh_token.

    Return access_token on success, None otherwise.
    """
    cache_file = _auth_cache_path(email)
    if not os.path.exists(cache_file):
        logger.debug("No auth cache found")
        return None

    try:
        with open(cache_file, "r") as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Could not read auth cache: {e}")
        return None

    refresh_token = cache.get("refresh_token")
    if not refresh_token:
        logger.debug("No refresh_token in cache")
        return None

    logger.debug("Attempting token refresh")
    oauth_cfg = _load_oauth_config()
    data = {
        "grant_type": "refresh_token",
        "client_id": oauth_cfg["client_id"],
        "audience": oauth_cfg["audience"],
        "refresh_token": refresh_token,
    }
    r = requests.post(
        WETRANSFER_AUTH0_URL,
        headers={
            "User-Agent": WETRANSFER_USER_AGENT,
            "Content-Type": "application/json",
        },
        json=data,
    )
    if r.status_code != 200:
        logger.debug(f"Token refresh failed ({r.status_code}): {r.text[:200]}")
        return None

    token_data = r.json()
    new_access = token_data.get("access_token")
    new_refresh = token_data.get("refresh_token", refresh_token)
    if new_access:
        _save_auth_cache(email, new_access, new_refresh)
        logger.debug("Token refresh successful")
        return new_access

    return None


def _authenticate_otp(email: str) -> Dict[str, str]:
    """Authenticate via WeTransfer passwordless OTP flow.

    Triggers a verification code to the given email, prompts the user
    to enter it, and exchanges it for Auth0 tokens.

    Return dict with "access_token" and optionally "refresh_token".
    """
    oauth_cfg = _load_oauth_config()

    logger.debug(f"Requesting OTP code for {email}")
    data = {"client_id": oauth_cfg["client_id"], "email": email}
    r = requests.post(
        "https://wetransfer.com/adroit/api/v1/login/passwordless",
        json=data,
    )
    r.raise_for_status()

    code = input("Enter the verification code sent to your email: ")

    logger.debug("Exchanging OTP for access_token")
    data = {
        "grant_type": "http://auth0.com/oauth/grant-type/passwordless/otp",
        "client_id": oauth_cfg["client_id"],
        "audience": oauth_cfg["audience"],
        "otp": code,
        "realm": "email",
        "username": email,
        "scope": "openid offline_access",
    }
    r = requests.post(
        WETRANSFER_AUTH0_URL,
        headers={
            "User-Agent": WETRANSFER_USER_AGENT,
            "x-csrf-token": "csrf-token",
        },
        json=data,
    )
    r.raise_for_status()
    logger.debug("OTP authentication successful")
    token_data = r.json()
    result = {"access_token": token_data["access_token"]}
    if "refresh_token" in token_data:
        result["refresh_token"] = token_data["refresh_token"]
        logger.debug("Obtained refresh_token for caching")
    else:
        logger.debug("No refresh_token returned by Auth0")
    return result


def _authenticate(email: str) -> str:
    """Authenticate with WeTransfer.

    Tries cached refresh_token first (no user interaction needed).
    Falls back to passwordless OTP if no cache or refresh fails.
    Caches tokens after successful OTP for future runs.

    Return access_token.
    """
    token = _load_cached_auth(email)
    if token:
        return token

    logger.debug("No valid cached token, starting OTP flow")
    otp_result = _authenticate_otp(email)
    _save_auth_cache(
        email,
        otp_result["access_token"],
        otp_result.get("refresh_token"),
    )
    return otp_result["access_token"]


def _prepare_link_upload(
    filenames: List[str],
    display_name: str,
    message: str,
    session: requests.Session,
    authenticated: bool = False,
) -> Dict[Any, Any]:
    """Given a list of filenames and a message prepare for the link upload.

    When authenticated is False, creates an anonymous transfer.
    When authenticated is True, creates a transfer under the logged-in account.

    Return the parsed JSON response.
    """
    j = {
        "files": [_file_name_and_size(f) for f in filenames],
        "display_name": display_name,
        "message": message,
        "ui_language": "en",
    }
    if not authenticated:
        j["anonymous_transfer"] = True

    r = session.post(WETRANSFER_API_URL, json=j)
    r.raise_for_status()
    return r.json()


def _storm_urls(
    authorization: str,
) -> Dict[str, str]:
    """Given an authorization bearer extract storm URLs.

    Return a dict with the various storm URLs.

    XXX: Here we can basically ask/be redirected anywhere. Should we do some
    XXX: possible sanity check to possibly avoid doing HTTP request to
    XXX: arbitrary URLs?
    """
    # Extract JWT payload and add extra padding to be sure that it can be
    # base64-decoded.
    j = json.loads(binascii.a2b_base64(authorization.split(".")[1] + "=="))
    return {
        "WETRANSFER_STORM_PREFLIGHT": j.get("storm.preflight_batch_url"),
        "WETRANSFER_STORM_BLOCK": j.get("storm.announce_blocks_url"),
        "WETRANSFER_STORM_BATCH": j.get("storm.create_batch_url"),
    }


def _storm_preflight_item(
    file: str,
) -> Dict[str, Union[List[Dict[str, int]], str]]:
    """Given a file, prepare the item block dictionary.

    Return a dictionary with "blocks", "item_type" and "path" keys.
    """
    filename = os.path.basename(file)
    filesize = os.path.getsize(file)

    return {
        "blocks": [{"content_length": filesize}],
        "item_type": "file",
        "path": filename,
    }


def _storm_preflight(
    authorization: str, filenames: List[str]
) -> Dict[Any, Any]:
    """Given an Authorization token and filenames do preflight for upload.

    Return the parsed JSON response.
    """
    j = {
        "items": [_storm_preflight_item(f) for f in filenames],
    }
    requests.options(
        _storm_urls(authorization)["WETRANSFER_STORM_PREFLIGHT"],
        headers={
            "Origin": "https://wetransfer.com",
            "Access-Control-Request-Method": "POST",
            "User-Agent": WETRANSFER_USER_AGENT,
        },
    )
    r = requests.post(
        _storm_urls(authorization)["WETRANSFER_STORM_PREFLIGHT"],
        json=j,
        headers={
            "Authorization": f"Bearer {authorization}",
            "User-Agent": WETRANSFER_USER_AGENT,
        },
    )
    return r.json()


def _md5(file: str) -> str:
    """Given a file, calculate its MD5 checksum.

    Return MD5 digest as str.
    """
    h = hashlib.md5()
    with open(file, "rb") as f:
        for chunk in iter(functools.partial(f.read, 4096), b""):
            h.update(chunk)
    return h.hexdigest()


def _storm_prepare_item(file: str) -> Dict[str, Union[int, str]]:
    """Given a file, prepare the block for blocks dictionary.

    Return a dictionary with "content_length" and "content_md5_hex" keys.
    """
    filesize = os.path.getsize(file)

    return {"content_length": filesize, "content_md5_hex": _md5(file)}


def _storm_prepare(authorization: str, filenames: List[str]) -> Dict[Any, Any]:
    """Given an Authorization token and filenames prepare for block uploads.

    Return the parsed JSON response.
    """
    j = {
        "blocks": [_storm_prepare_item(f) for f in filenames],
    }
    requests.options(
        _storm_urls(authorization)["WETRANSFER_STORM_BLOCK"],
        headers={
            "Origin": "https://wetransfer.com",
            "Access-Control-Request-Method": "POST",
            "User-Agent": WETRANSFER_USER_AGENT,
        },
    )
    r = requests.post(
        _storm_urls(authorization)["WETRANSFER_STORM_BLOCK"],
        json=j,
        headers={
            "Authorization": f"Bearer {authorization}",
            "Origin": "https://wetransfer.com",
            "User-Agent": WETRANSFER_USER_AGENT,
        },
    )
    return r.json()


def _storm_finalize_item(
    file: str, block_id: str
) -> Dict[str, Union[List[str], str]]:
    """Given a file and block_id prepare the item block dictionary.

    Return a dictionary with "block_ids", "item_type" and "path" keys.

    XXX: Is it possible to actually have more than one block?
    XXX: If yes this - and probably other parts of the code involved with
    XXX: blocks - needs to be instructed to handle them instead of
    XXX: assuming that one file is associated with one block.
    """
    filename = os.path.basename(file)

    return {
        "block_ids": [
            block_id,
        ],
        "item_type": "file",
        "path": filename,
    }


def _storm_finalize(
    authorization: str, filenames: List[str], block_ids: List[str]
) -> Dict[Any, Any]:
    """Given an Authorization token, filenames and block ids finalize upload.

    Return the parsed JSON response.
    """
    j = {
        "items": [
            _storm_finalize_item(f, bid)
            for f, bid in zip(filenames, block_ids)
        ],
    }
    requests.options(
        _storm_urls(authorization)["WETRANSFER_STORM_BATCH"],
        headers={
            "Origin": "https://wetransfer.com",
            "Access-Control-Request-Method": "POST",
            "User-Agent": WETRANSFER_USER_AGENT,
        },
    )

    for i in range(0, 5):
        r = requests.post(
            _storm_urls(authorization)["WETRANSFER_STORM_BATCH"],
            json=j,
            headers={
                "Authorization": f"Bearer {authorization}",
                "Origin": "https://wetransfer.com",
                "User-Agent": WETRANSFER_USER_AGENT,
            },
        )
        if r.status_code == 200:
            break
        else:
            # HTTP request can have 425 HTTP status code and fails with
            # error_code 'BLOCKS_STILL_EXPECTED'. Retry in that and any
            # non-200 cases.
            logger.debug(
                "Request against "
                + f"{_storm_urls(authorization)['WETRANSFER_STORM_BATCH']} "
                + f"returned {r.status_code}, retrying in {2 ** i} seconds"
            )
            time.sleep(2**i)

    return r.json()


def _storm_upload(url: str, file: str) -> None:
    """Given an url and file upload it.

    Does not return anything.
    """
    requests.options(
        url,
        headers={
            "Origin": "https://wetransfer.com",
            "Access-Control-Request-Method": "PUT",
            "User-Agent": WETRANSFER_USER_AGENT,
        },
    )
    with open(file, "rb") as f:
        requests.put(
            url,
            data=f,
            headers={
                "Origin": "https://wetransfer.com",
                "Content-MD5": binascii.b2a_base64(
                    binascii.unhexlify(_md5(file)), newline=False
                ),
                "X-Uploader": "storm",
                "User-Agent": WETRANSFER_USER_AGENT,
            },
        )


def _finalize_upload(
    transfer_id: str, session: requests.Session
) -> Dict[Any, Any]:
    """Given a transfer_id finalize the upload.

    Return the parsed JSON response.
    """
    j = {
        "wants_storm": True,
    }
    r = session.put(
        WETRANSFER_FINALIZE_URL.format(transfer_id=transfer_id), json=j
    )

    return r.json()


def auth_list() -> None:
    """List all cached WeTransfer accounts and their token status."""
    import glob
    import datetime

    pattern = os.path.join(WETRANSFER_AUTH_CACHE_DIR, "auth_*.json")
    cache_files = glob.glob(pattern)

    if not cache_files:
        print("No cached accounts found.")
        print(f"  Cache directory: {WETRANSFER_AUTH_CACHE_DIR}")
        print('  Run "transferwee auth <email>" to authenticate.')
        return

    for cache_file in sorted(cache_files):
        try:
            with open(cache_file, "r") as f:
                cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        email = cache.get("email", "unknown")
        has_refresh = bool(cache.get("refresh_token"))

        token_info = ""
        access_token = cache.get("access_token", "")
        try:
            payload = json.loads(
                binascii.a2b_base64(access_token.split(".")[1] + "==")
            )
            iat = datetime.datetime.utcfromtimestamp(payload.get("iat", 0))
            exp = datetime.datetime.utcfromtimestamp(payload.get("exp", 0))
            now = datetime.datetime.utcnow()
            if now < exp:
                token_info = f"access_token valid until {exp:%Y-%m-%d %H:%M} UTC"
            else:
                token_info = f"access_token expired ({exp:%Y-%m-%d %H:%M} UTC)"
            token_info += f", last refreshed {iat:%Y-%m-%d %H:%M} UTC"
        except Exception:
            token_info = "could not decode token"

        status = "refresh_token cached" if has_refresh else "no refresh_token"
        print(f"  {email}")
        print(f"    status: {status}")
        print(f"    {token_info}")
        print(f"    file:   {cache_file}")
        print()


def auth(
    email: str,
    client_id: Optional[str] = None,
    audience: Optional[str] = None,
) -> None:
    """Authenticate with WeTransfer and cache tokens for future use.

    Triggers the OTP flow (a verification code is sent to the given
    email) and stores the resulting tokens in the local cache.
    Subsequent upload calls with the same email will use the cached
    refresh_token without requiring user interaction.

    If client_id or audience are provided they are persisted to
    oauth_config.json and used for this and all future auth flows.
    """
    if client_id or audience:
        current = _load_oauth_config()
        _save_oauth_config(
            client_id or current["client_id"],
            audience or current["audience"],
        )
        logger.info(f"OAuth config saved to {WETRANSFER_OAUTH_CONFIG}")

    cached = _load_cached_auth(email)
    if cached:
        logger.info(f"Already authenticated as {email} (token refreshed)")
        return

    logger.info(f"Sending verification code to {email}")
    otp_result = _authenticate_otp(email)
    _save_auth_cache(
        email,
        otp_result["access_token"],
        otp_result.get("refresh_token"),
    )
    if otp_result.get("refresh_token"):
        logger.info(
            f"Authentication successful. Tokens cached to "
            f"{_auth_cache_path(email)}"
        )
    else:
        logger.info(
            "Authentication successful but no refresh_token was returned. "
            "You will need to re-authenticate on every upload."
        )


def upload(
    files: List[str],
    display_name: str = "",
    message: str = "",
    sender: Optional[str] = None,
    recipients: Optional[List[str]] = [],
    user: Optional[str] = None,
) -> str:
    """Given a list of files upload them and return the corresponding URL.

    Also accepts optional parameters:
     - `display_name': name used as a title of the transfer
     - `message': message used as a description of the transfer
     - `sender': email address used to receive an ACK if the upload is
                 successfull. For every download by the recipients an email
                 will be also sent
     - `recipients': list of email addresses of recipients. When the upload
                     succeed every recipients will receive an email with a link
     - `user': WeTransfer account email for authenticated uploads (a
               verification code will be sent to this email)

    If both sender and recipient parameters are passed the email upload will be
    used. Otherwise, the link upload will be used.

    When user is provided the upload is performed as an authenticated user,
    which may lift anonymous transfer limits.

    Return the short URL of the transfer on success.
    """

    # Check that all files exists
    logger.debug("Checking that all files exists")
    for f in files:
        if not os.path.exists(f):
            raise FileNotFoundError(f)

    # Check that there are no duplicates filenames
    # (despite possible different dirname())
    logger.debug("Checking for no duplicate filenames")
    filenames = [os.path.basename(f) for f in files]
    if len(files) != len(set(filenames)):
        raise FileExistsError("Duplicate filenames")

    # Authenticate if credentials were provided
    auth_token = None
    if user:
        logger.debug(f"Authenticating as {user}")
        auth_token = _authenticate(user)
        logger.debug("Authentication successful")

    logger.debug("Preparing to upload")
    transfer = None
    s = _prepare_session()
    if not s:
        raise ConnectionError("Could not prepare session")

    if auth_token:
        s.headers.update({"Authorization": f"Bearer {auth_token}"})

    if sender and recipients:
        # email upload
        transfer = _prepare_email_upload(
            files, display_name, message, sender, recipients, s
        )
        transfer = _verify_email_upload(transfer, s)
    else:
        # link upload
        transfer = _prepare_link_upload(
            files, display_name, message, s,
            authenticated=auth_token is not None,
        )

    logger.debug(
        "From storm_upload_token WETRANSFER_STORM_PREFLIGHT URL is: "
        + _storm_urls(transfer["storm_upload_token"])[
            "WETRANSFER_STORM_PREFLIGHT"
        ],
    )
    logger.debug(
        "From storm_upload_token WETRANSFER_STORM_BLOCK URL is: "
        + _storm_urls(transfer["storm_upload_token"])[
            "WETRANSFER_STORM_BLOCK"
        ],
    )
    logger.debug(
        "From storm_upload_token WETRANSFER_STORM_BLOCK URL is: "
        + _storm_urls(transfer["storm_upload_token"])[
            "WETRANSFER_STORM_BATCH"
        ],
    )
    logger.debug(f"Get transfer id {transfer['id']}")
    logger.debug("Doing preflight storm")
    _storm_preflight(transfer["storm_upload_token"], files)
    logger.debug("Preparing storm block upload")
    blocks = _storm_prepare(transfer["storm_upload_token"], files)
    for f, b in zip(files, blocks["data"]["blocks"]):
        logger.debug(f"Uploading file {f}")
        _storm_upload(b["presigned_put_url"], f)
    logger.debug("Finalizing storm batch upload")
    _storm_finalize(
        transfer["storm_upload_token"],
        files,
        [b["block_id"] for b in blocks["data"]["blocks"]],
    )
    logger.debug(f"Finalizing upload with transfer id {transfer['id']}")
    shortened_url = _finalize_upload(transfer["id"], s)["shortened_url"]
    _close_session(s)
    return shortened_url


if __name__ == "__main__":
    from sys import exit
    import argparse

    log = logging.getLogger(__name__)
    log.setLevel(logging.INFO)
    log.addHandler(logging.StreamHandler())

    ap = argparse.ArgumentParser(
        prog="transferwee",
        description="Download/upload files via wetransfer.com",
    )
    sp = ap.add_subparsers(dest="action", help="action", required=True)

    # download subcommand
    dp = sp.add_parser("download", help="download files")
    dp.add_argument(
        "-g",
        action="store_true",
        help="only print the direct link (without downloading it)",
    )
    dp.add_argument(
        "-o",
        type=str,
        default="",
        metavar="file",
        help="output file to be used",
    )
    dp.add_argument(
        "-v", action="store_true", help="get verbose/debug logging"
    )
    dp.add_argument(
        "url",
        nargs="+",
        type=str,
        metavar="url",
        help="URL (we.tl/... or wetransfer.com/downloads/...)",
    )

    # auth subcommand
    authp = sp.add_parser(
        "auth",
        help="authenticate with WeTransfer (OTP via email)",
    )
    authp.add_argument(
        "email",
        nargs="?",
        type=str,
        metavar="email",
        help="WeTransfer account email to authenticate",
    )
    authp.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="list cached accounts and token status",
    )
    authp.add_argument(
        "--client-id",
        type=str,
        metavar="ID",
        help="override Auth0 client_id (saved to oauth_config.json)",
    )
    authp.add_argument(
        "--audience",
        type=str,
        metavar="URL",
        help="override Auth0 audience (saved to oauth_config.json)",
    )
    authp.add_argument(
        "-v", action="store_true", help="get verbose/debug logging"
    )

    # upload subcommand
    up = sp.add_parser("upload", help="upload files")
    up.add_argument(
        "-n",
        type=str,
        default="",
        metavar="display_name",
        help="title for the transfer",
    )
    up.add_argument(
        "-m",
        type=str,
        default="",
        metavar="message",
        help="message description for the transfer",
    )
    up.add_argument("-f", type=str, metavar="from", help="sender email")
    up.add_argument(
        "-t", nargs="+", type=str, metavar="to", help="recipient emails"
    )
    up.add_argument(
        "-u",
        "--user",
        type=str,
        default=os.environ.get("WETRANSFER_USER"),
        metavar="email",
        help="WeTransfer account email (or WETRANSFER_USER env var)",
    )
    up.add_argument(
        "-v", action="store_true", help="get verbose/debug logging"
    )
    up.add_argument(
        "files", nargs="+", type=str, metavar="file", help="files to upload"
    )

    args = ap.parse_args()

    if args.v:
        log.setLevel(logging.DEBUG)

    if args.action == "auth":
        if args.list:
            auth_list()
        elif args.email:
            auth(args.email, args.client_id, args.audience)
        else:
            authp.print_help()
        exit(0)

    if args.action == "download":
        if args.g:
            for u in args.url:
                print(download_url(u))
        else:
            for u in args.url:
                download(u, args.o)
        exit(0)

    if args.action == "upload":
        print(
            upload(
                args.files, args.n, args.m, args.f, args.t,
                args.user,
            )
        )
        exit(0)
