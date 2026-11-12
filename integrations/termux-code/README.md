# termux-code integration

The termux-code gate (https://termux-code.pages.dev) already hands Jay
ONE copy-paste Termux command carrying his per-owner gateway token:

```
pkg install nodejs git
curl -fsSL <site>/install.sh | TERMUX_CODE_GATEWAY=<site> TERMUX_CODE_TOKEN=<token> bash
termux-code
```

The bridge rides the exact same pattern, with the SAME token. The
Linux box prints the exact command at startup, real address and key
already filled in (see the main README's Install section); copy that
block and run it on the phone:

```
curl -fsSL http://LAN_ADDRESS_FROM_SERVER_STARTUP:8765/install-termux.sh | MONICO_SERVER=http://LAN_ADDRESS_FROM_SERVER_STARTUP:8765 MONICO_TOKEN=PAIRING_KEY_FROM_SERVER_STARTUP bash
monico-connect
```

## How the token is shared

- The bridge server has its own pairing key (linux-remote pattern:
  generated once, 0600 file).
- It can additionally accept the termux-code gateway token. On the
  Linux box, run once:

  ```
  monico-server token-add <tc_v1 token>
  ```

  The token is stored 0600 in `~/.monico-bridge/accepted_tokens` and
  works from then on as a bridge pairing key. Rotating the gateway
  token (the termux-code revoke path) means re-running token-add with
  the new one.

## Gate side (patch, NOT deployed)

`tokenpanel-bridge.patch` patches `app/TokenPanel.jsx` in
jaykk99/termux-code: when the site is built with
`NEXT_PUBLIC_MONICO_SERVER=<bridge URL>`, the mint panel shows a second
copy-paste block, "Monico Labs bridge (phone as a server)", carrying
the same freshly minted token in `MONICO_TOKEN`. Verified: the patch
applies cleanly against the current TokenPanel.jsx.

Applying it to the live gate needs a termux-code deploy, which is
Jay's tap. Until then the two commands are run separately, both shown
in the bridge README.
