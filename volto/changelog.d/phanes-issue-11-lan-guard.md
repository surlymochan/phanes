### Fixed

- The development LAN bind refuses to open while the local Auth API returns sign-in
  codes, so no caller of the bind can open the network with the code readable.
