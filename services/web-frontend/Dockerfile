# services/web-frontend/Dockerfile -- TaskFlow
# Stage 1 builds the React app with Vite; stage 2 serves the static files
# with nginx as the unprivileged "nginx" user (UID 101) on port 8080.

FROM node:20.18.1-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY index.html vite.config.js ./
COPY src ./src
RUN npm run build

FROM nginx:1.27.3-alpine
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html
USER 101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
