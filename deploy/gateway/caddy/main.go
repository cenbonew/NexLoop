// NexLoop gateway: the standard Caddy distribution (the modules of the official image), rebuilt with a patched Go toolchain and
// golang.org/x/net when the upstream release binary carries fixed Critical findings (NX-036; deploy/gateway/Dockerfile).
package main

import (
	caddycmd "github.com/caddyserver/caddy/v2/cmd"

	_ "github.com/caddyserver/caddy/v2/modules/standard"
)

func main() {
	caddycmd.Main()
}
