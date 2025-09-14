# ubuntu-22.04/Dockerfile
FROM ubuntu:22.04

RUN apt-get update && \
    apt-get install -y \
    openssh-server \
    wget \
    curl \
    git \
    tmate && \
    rm -rf /var/lib/apt/lists/*

# Set up SSH
RUN mkdir /var/run/sshd
RUN echo 'root:password' | chpasswd
RUN sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config

# Configure tmate
RUN mkdir -p /root/.tmate
COPY tmate.conf /root/.tmate/

EXPOSE 22
CMD ["/usr/sbin/sshd", "-D"]