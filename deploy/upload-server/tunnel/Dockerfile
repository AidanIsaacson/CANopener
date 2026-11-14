# The SSH tunnel beside remote help: an sshd that lets enrolled units open one reverse tunnel each
# and nothing else (no shell, no passwords). See ../README.md, "SSH tunnel".
FROM alpine:3.20
RUN apk add --no-cache openssh-server && adduser -D -s /sbin/nologin tunnel && passwd -u tunnel
COPY sshd_config /etc/ssh/sshd_config
COPY start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/start.sh"]
