#!/bin/sh
set -e
/www/server/nginx/sbin/nginx -t
/www/server/nginx/sbin/nginx -s reload
