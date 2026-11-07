FROM coturn/coturn:4.17.2-r0
COPY remote/coturn/turnserver.conf /etc/coturn/volto.conf
COPY remote/coturn/entrypoint.sh /usr/local/bin/volto-coturn
ENTRYPOINT ["/usr/local/bin/volto-coturn"]
