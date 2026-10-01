# Architecture

## Overview

The HedgeFund system is built on a microservices architecture with the following layers:

- **Frontend**: React + TypeScript with Vite
- **Backend**: Node.js with Express
- **Data Layer**: PostgreSQL with Supabase
- **Cache**: Redis
- **Messaging**: RabbitMQ

## Key Components

- **API Gateway**: Routes requests to appropriate services
- **Auth Service**: JWT-based authentication
- **Portfolio Service**: Managing investment portfolios
- **Risk Service**: Risk assessment and monitoring
- **Reporting Service**: Generating performance reports