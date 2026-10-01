# Instructions
AI Trading Bot Architecture Audit Request

I need a complete technical and business overview of the current trading bot architecture before implementing major upgrades.

Please analyze the existing codebase and provide detailed answers to the following sections.

1. Project Overview

Provide:

Plain Text
Project Name:
 
Primary Purpose:
 
Current Version:
 
Development Status:
- Prototype
- Beta
- Production
 
Supported Markets:
- Forex
- Crypto
- Indices
- Commodities
- Stocks
 
Supported Brokers:
 
Trading Platform:
- MT5
- MT4
- cTrader
- TradingView
- Other
Show more lines
2. Technology Stack

Provide complete stack details.

Plain Text
Frontend:
 
Framework:
Language:
State Management:
UI Library:
Chart Library:
CSS Framework:
Animation Library:
 
Backend:
 
Language:
Framework:
Database:
ORM:
Queue:
 
Real-Time Communication:
 
WebSocket:
SSE:
Polling:
 
Authentication:
 
Deployment:
 
Cloud Provider:
Docker:
CI/CD:
Show more lines
3. Current System Architecture

Explain:

Plain Text
How data flows through the system.
 
Market Data
↓
Indicators
↓
Signal Generation
↓
Risk Manager
↓
Trade Execution
↓
Trade Monitoring
 
Provide actual architecture diagram if available.
Show more lines
4. MT5 / Broker Integration

Provide:

Plain Text
Broker Connection Method
 
MT5 Terminal
MetaApi
Direct API
Custom Bridge
 
Current Connection Stability
 
Reconnect Logic
 
Execution Latency
 
Supported Order Types
 
Market
Limit
Stop
Stop Limit
Show more lines
5. Trading Engine

Describe current logic.

Entry Logic
Plain Text
How does the bot decide when to BUY?
 
Which indicators are used?
 
Which conditions must be true?
Show more lines
Exit Logic
Plain Text
Take Profit Logic
 
Stop Loss Logic
 
Manual Exit?
 
Auto Exit?
Show more lines
Signal Process
Plain Text
Rule-Based?
ML-Based?
AI-Based?
Hybrid?
``
Show more lines
6. Indicators Currently Implemented

Mark each:

Plain Text
EMA20
EMA50
EMA100
EMA200
 
RSI
 
MACD
 
ATR
 
ADX
 
VWAP
 
Bollinger Bands
 
Volume Profile
 
SuperTrend
 
Ichimoku
 
Stochastic
 
Market Structure
 
Liquidity Zones
 
Fair Value Gaps
 
Order Blocks
 
Supply And Demand
 
Smart Money Concepts
Show more lines

For each indicator explain:

Plain Text
Implemented?
Used?
Where?
``
Show more lines
7. Symbol Discovery Capability

Can the bot currently:

Plain Text
Trade only selected symbol?
 
Scan watchlist?
 
Scan all broker symbols?
 
Multi-market scanning?
 
Ranking system?
Show more lines

Provide:

Plain Text
Maximum symbols scanned simultaneously.
Show more lines
8. Autonomous Trading Capability

Current status:

Plain Text
Signal Generation Only
 
Semi Auto Trading
 
Fully Automated Trading
Show more lines

Can the bot currently:

Plain Text
Open trades automatically?
 
Close trades automatically?
 
Adjust stop losses?
 
Move to breakeven?
 
Apply trailing stops?
 
Partial profit booking?
 
Scale in?
 
Scale out?
Show more lines
9. Risk Management

Current settings.

Plain Text
Risk Per Trade
 
Position Sizing Method
 
Max Open Trades
 
Daily Loss Limit
 
Max Drawdown
 
Max Exposure
 
Correlation Control
 
Spread Filter
 
Slippage Protection
Show more lines

Provide actual formulas used.

10. Position Sizing Engine

Explain:

Plain Text
Fixed Lot
 
Balance Based
 
Equity Based
 
ATR Based
 
Risk Based
 
Kelly Criterion
Show more lines

Provide current calculation examples.

11. Existing Dashboard Modules

List every current module.

Example:

Plain Text
Account Summary
 
Balance Widget
 
Equity Widget
 
P&L Monitor
 
Position Table
 
Chart Panel
 
Signal Generator
 
AI Brain Reactor
 
Execution Console
 
Risk Matrix
Show more lines
12. Real-Time Data Architecture

How are updates delivered?

Plain Text
WebSocket
 
Polling
 
SSE
 
Refresh Interval
 
Data Frequency
 
Maximum Throughput
Show more lines
13. AI Layer Analysis

Very important.

Explain exactly.

Current AI type:

Plain Text
No AI
 
Indicator Rules
 
Weighted Scoring Model
 
Machine Learning
 
Deep Learning
 
LLM Integration
Show more lines

If AI exists:

Plain Text
Model Used
 
Training Data
 
Features
 
Inference Frequency
 
Accuracy Statistics
 
Show more lines
14. Strategy Framework

List all strategies currently available.

Example:

Plain Text
Trend Following
 
Momentum
 
Breakout
 
Scalping
 
Mean Reversion
 
Swing Trading
 
Grid Trading
 
News Trading
Show more lines

For every strategy provide:

Plain Text
Entry Conditions
 
Exit Conditions
 
Win Rate
 
Backtest Results
Show more lines
15. Trade Journal

Does system store:

Plain Text
Entry
 
Exit
 
Reason For Entry
 
Strategy Used
 
Profit/Loss
 
Screenshots
 
Trade Duration
Show more lines

Storage location:

Plain Text
Database
CSV
Logs
None
Show more lines
16. Backtesting System

Current capabilities:

Plain Text
Historical Data Source
 
Date Range
 
Model Quality
 
Walk Forward Testing
 
Monte Carlo
 
Parameter Optimization
Show more lines

Provide recent backtest results.

17. Performance Analytics

Available metrics:

Plain Text
Win Rate
 
Profit Factor
 
Sharpe Ratio
 
Sortino Ratio
 
Recovery Factor
 
Expectancy
 
Maximum Drawdown
 
Risk Reward Statistics
Show more lines
18. Current Limitations

Please identify:

Plain Text
Technical bottlenecks
 
Execution bottlenecks
 
Performance bottlenecks
 
UI limitations
 
Scalability limitations
 
Security concerns
 
Trading weaknesses
Show more lines
19. Feature Readiness Assessment

Can the current architecture support:

Plain Text
Autonomous Market Scanner
 
Multi-Symbol Trading
 
AI Confidence Engine
 
Strategy Marketplace
 
Portfolio Manager
 
Trade Journal
 
Advanced Analytics
 
Risk Engine v2
 
TradingView Charts
 
Lightning Border UI
 
WebSockets
 
Mobile Dashboard
 
Paper Trading
 
Simulation Mode
Show more lines

For each answer:

Plain Text
Ready Now
Minor Changes Needed
Major Refactor Needed
``
Show more lines
20. Repository Analysis

Provide:

Plain Text
Project Structure
 
Major Components
 
Core Services
 
Trading Engine Files
 
Broker Files
 
Risk Engine Files
 
Dashboard Files
 
Chart Files
 
API Endpoints
Show more lines
21. Recommended Roadmap

Act as:

Senior Quant Developer
Senior Trading Systems Architect
Senior UI/UX Architect

Provide:

Plain Text
Top 10 improvements
 
Highest risk areas
 
Highest ROI upgrades
 
Must-fix bugs
 
Architecture improvements
 
Performance improvements
 
UI improvements
Show more lines
Final Deliverable

Provide a complete architecture review report with:

Plain Text
Current Capability Score: /100
 
UI Score: /100
 
Trading Logic Score: /100
 
Risk Management Score: /100
 
Scalability Score: /100
 
Code Quality Score: /100
 
Recommended Next Version Features
 
Priority Order:
P0 Critical
P1 Important
P2 Nice To Have
Show more lines

This report should be based on the actual codebase analysis, not assumptions. Include code references, file paths, architecture diagrams, and implementation recommendations wherever possible.