# Instructions_1

## Additional Setup Steps

1. Database migration: `npx supabase migration run`
2. Seed data: `npm run seed`
3. Initialize caches: `redis-server --daemonize yes`
4. Verify services: `npm run check:services`

## Troubleshooting

- If services fail to start, check the logs: `npm run logs`
- For connection issues, verify environment variables