FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
RUN apk add --no-cache openssl
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.http.conf /etc/nginx/http.conf
COPY nginx.https.conf /etc/nginx/https.conf
COPY docker-entrypoint.sh /snapman-entrypoint.sh
RUN chmod +x /snapman-entrypoint.sh
EXPOSE 80
ENTRYPOINT ["/snapman-entrypoint.sh"]
