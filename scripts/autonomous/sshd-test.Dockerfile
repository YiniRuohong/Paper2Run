FROM paper2run-experiment:1
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh \
    && ssh-keygen -A \
    && printf '%s\n' 'PermitRootLogin prohibit-password' 'PasswordAuthentication no' 'AllowAgentForwarding no' 'AllowTcpForwarding no' 'X11Forwarding no' >> /etc/ssh/sshd_config
EXPOSE 22
CMD ["/usr/sbin/sshd","-D","-e"]
