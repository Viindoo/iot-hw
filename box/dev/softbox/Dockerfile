# The software box: the file layout of a Viindoo IoT Box on Debian, for a Linux x86 workstation.
# Build it with "softbox.sh build", which reads both package lists from the image script of core.

FROM debian:bookworm-slim

ARG APT_PACKAGES
ARG PIP_PACKAGES

ENV DEBIAN_FRONTEND=noninteractive

RUN test -n "${APT_PACKAGES}" && test -n "${PIP_PACKAGES}" \
    && apt-get update \
    && apt-get install -y --no-install-recommends ${APT_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages ${PIP_PACKAGES}

RUN echo "en_US.UTF-8 UTF-8" > /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8 LANGUAGE=en_US.UTF-8 LC_ALL=en_US.UTF-8

# The box user and its groups, as the image script of core and the operating system of a box set them.
RUN useradd --create-home --uid 1000 --shell /bin/bash pi \
    && for group in usbusers lp lpadmin dialout input; do groupadd --force "${group}"; done \
    && usermod --append --groups usbusers,lp,lpadmin,dialout,input pi \
    && mkdir -p /var/log/odoo /var/odoo /var/lib/softbox/prints \
    && chown pi:pi /var/log/odoo /var/lib/softbox \
    && chown lp:lp /var/lib/softbox/prints

# A box reaches its card through /root_bypass_ramdisks while /etc and /var live in memory.
# The software box has one file system, so both names lead to the same files.
RUN ln -s / /root_bypass_ramdisks

# The core repository is mounted from the host and belongs to whoever owns it there.
RUN git config --system --add safe.directory /mnt/core \
    && git config --system --add safe.directory /mnt/core/.git

# Each file is copied on its own: copying a directory would give the system directories
# it lands in the permissions of the build context, which the print server refuses.
COPY --chmod=0755 container/entrypoint.sh /usr/local/bin/softbox-entrypoint
COPY --chmod=0755 container/print-backend.sh /usr/lib/cups/backend/softbox
COPY --chmod=0440 container/sudoers /etc/sudoers.d/010_pi-nopasswd
COPY iot_handlers/ /usr/share/softbox/iot_handlers/
RUN chmod -R u=rwX,go=rX /usr/share/softbox

ENTRYPOINT ["/usr/local/bin/softbox-entrypoint"]
CMD ["box"]
