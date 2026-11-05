FROM node:22-bookworm-slim
ARG GENIEACS_VERSION=1.2.16
RUN npm install -g "genieacs@${GENIEACS_VERSION}" && npm cache clean --force
USER node
ENV GENIEACS_LOG_FORMAT=json GENIEACS_ACCESS_LOG_FORMAT=json
CMD ["genieacs-cwmp"]
