# API Reference

## Base URL

```
http://localhost:3000/api
```

## Authentication

All API requests must include an Authorization header:
```
Authorization: Bearer <jwt-token>
```

## Endpoints

### Health Check

`GET /health` - Check API status

### Portfolios

`GET /portfolios` - List all portfolios
`POST /portfolios` - Create a new portfolio
`GET /portfolios/:id` - Get portfolio details
`PUT /portfolios/:id` - Update portfolio
`DELETE /portfolios/:id` - Delete portfolio