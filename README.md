# transferwee

transferwee is a simple Python 3 script to download/upload files via
[wetransfer.com](https://wetransfer.com/).

## Usage

```
% transferwee -h
usage: transferwee [-h] {download,auth,upload} ...

Download/upload files via wetransfer.com

positional arguments:
  {download,auth,upload}
                        action
    download            download files
    auth                authenticate with WeTransfer (OTP via email)
    upload              upload files

optional arguments:
  -h, --help            show this help message and exit
```

### Authenticate

`auth` subcommand authenticates with WeTransfer via a one-time
verification code sent to your email. The resulting tokens are cached
locally so that subsequent uploads can run without user interaction.

```
% transferwee auth -h
usage: transferwee auth [-h] [-l] [--client-id ID] [--audience URL] [-v] [email]

positional arguments:
  email           WeTransfer account email to authenticate

optional arguments:
  -h, --help      show this help message and exit
  -l, --list      list cached accounts and token status
  --client-id ID  override Auth0 client_id (saved to oauth_config.json)
  --audience URL  override Auth0 audience (saved to oauth_config.json)
  -v              get verbose/debug logging
```

The following example authenticates with WeTransfer. A verification code
will be sent to the given email address:

```
% transferwee auth user@example.com
Sending verification code to user@example.com
Enter the verification code sent to your email: ABC123
Authentication successful. Tokens cached to /home/user/.config/transferwee/auth_1a2b3c4d5e6f7890.json
```

Once authenticated, the cached refresh token is used automatically. To
check which accounts are cached:

```
% transferwee auth -l
  user@example.com
    status: refresh_token cached
    access_token valid until 2026-03-19 22:30 UTC, last refreshed 2026-03-19 21:30 UTC
    file:   /home/user/.config/transferwee/auth_1a2b3c4d5e6f7890.json
```

Multiple accounts are supported. Each account gets its own cache file
under `~/.config/transferwee/`.

### OAuth configuration

The Auth0 `client_id` and `audience` values used for authentication are
hardcoded with known-good defaults. If WeTransfer changes these values
on their end, the `--client-id` and `--audience` flags on the `auth`
subcommand allow overriding them without modifying the source code:

```
% transferwee auth --client-id NEW_ID --audience NEW_AUDIENCE user@example.com
```

The overrides are persisted to `~/.config/transferwee/oauth_config.json`
and will be used for all subsequent auth and upload operations. If the
config file does not exist, the built-in defaults are used.

### Upload files

`upload` subcommand uploads all the files and then print the shorten
URL corresponding the transfer.

If both `-f` option and `-t` option are passed the email upload
will be used (in that way the sender will get an email after the
upload and after every recipient will download the file, please
also note that because `-t` option accepts several fields a `--`
is needed to separate it with the file arguments).
Otherwise the link upload will be used.

When `-u` option is passed (or `WETRANSFER_USER` environment variable
is set) the upload is performed as an authenticated user, which avoids
the limits applied to anonymous transfers. Run `transferwee auth` first
to cache the tokens.

```
% transferwee upload -h
usage: transferwee upload [-h] [-n display_name] [-m message] [-f from]
                          [-t to [to ...]] [-u email] [--auth-file path]
                          [--expire-in duration] [-v]
                          file [file ...]

positional arguments:
  file                  files to upload

optional arguments:
  -h, --help            show this help message and exit
  -n display_name       title for the transfer
  -m message            message description for the transfer
  -f from               sender email
  -t to [to ...]        recipient emails
  -u email, --user email
                        WeTransfer account email (or WETRANSFER_USER env var)
  --auth-file path      path to auth cache JSON file (overrides -u and default
                        cache)
  --expire-in duration  transfer expiration, e.g. 3600, 90m, 24h, 30d
                        (default: 30d)
  -v                    get verbose/debug logging
```

The following example creates an `hello` text file with just `Hello world!` and
then upload it with the message passed via `-m` option:

```
% echo 'Hello world!' > hello
% md5 hello
MD5 (hello) = 59ca0efa9f5633cb0371bbc0355478d8
% transferwee upload -m 'Just a text file with the mandatory message...' hello
https://we.tl/o8mGUXnxyZ
```

Authenticated upload example:

```
% transferwee auth user@example.com
% transferwee upload -u user@example.com hello
https://we.tl/t-AbCdEfGhIj
```

Or using the environment variable:

```
% export WETRANSFER_USER=user@example.com
% transferwee upload hello
https://we.tl/t-AbCdEfGhIj
```

#### Using an external auth cache file

If you manage the auth cache outside the default `~/.config/transferwee/`
directory (e.g. in a Docker container or a CI pipeline), you can point
directly to a JSON cache file with `--auth-file`. This overrides both
`-u` and `WETRANSFER_USER`:

```
% transferwee upload --auth-file /path/to/auth_cache.json hello
https://we.tl/t-XyZwVuTsRq
```

#### Transfer expiration

By default, link uploads expire after 30 days. Use `--expire-in` to set
a custom duration. The value can be raw seconds or a human-readable
shorthand (`s` seconds, `m` minutes, `h` hours, `d` days):

```
% transferwee upload --expire-in 7d hello          # expires in 7 days
% transferwee upload --expire-in 12h hello         # expires in 12 hours
% transferwee upload --expire-in 3600 hello        # expires in 1 hour
```

### Download file

`download` subcommand download all the files from the given
we.tl/wetransfer.com URLs.

If the `-g` option is used it will just print the direct link
corresponding each URLs without downloading files.

The URL supported are the ones in the form:

- `https://we.tl/<short_url_id>`:
  received via link upload, via email to the sender and printed by
  `upload` action
- `https://wetransfer.com/<transfer_id>/<security_hash>`:
  directly not shared in any ways but the short URLs actually redirect to
  them
- `https://wetransfer.com/<transfer_id>/<recipient_id>/<security_hash>`:
  received via email by recipients when the files are shared via email
  upload

```
% transferwee download -h
usage: transferwee download [-h] [-g] [-o file] [-v] url [url ...]

positional arguments:
  url         URL (we.tl/... or wetransfer.com/downloads/...)

optional arguments:
  -h, --help  show this help message and exit
  -g          only print the direct link (without downloading it)
  -o file     output file to be used
  -v          get verbose/debug logging
```

The following example download the `hello` text file that was uploaded in the
previous example for `upload` subcommand. Please note that if any file with the
same name already exists it will be overwritten!:

```
% transferwee download https://we.tl/o8mGUXnxyZ
% cat hello
Hello world!
% md5 hello
MD5 (hello) = 59ca0efa9f5633cb0371bbc0355478d8
```

## Dependencies

transferwee needs [requests](http://python-requests.org/) package.
