# WAT (Wagering Account Transfer) Implementation Notes

## Understanding WAT in SAS Context

WAT (Wagering Account Transfer) is primarily a G2S protocol feature that allows transfers to/from player accounts on a host system. 

### Key Differences:
- **G2S**: Supports both embedded EGM-based user interfaces and external host-controlled interfaces
- **SAS**: Does NOT support embedded user interfaces for WAT

## Our Implementation Strategy

Since we're building a SAS-based system and SAS doesn't support embedded WAT interfaces, we'll implement player account functionality through:

1. **SMIB Touchscreen Interface**
   - Player login/logout on SMIB touchscreen
   - Account balance display
   - Transfer requests initiated from SMIB
   - PIN entry on SMIB touchscreen

2. **Host-Controlled Transfers**
   - Server initiates transfers based on SMIB requests
   - All account management done at server level
   - SAS commands used only for credit transfers (AFT)

3. **Hybrid Approach**
   - Use AFT commands for the actual credit transfers
   - Use network protocol for account authentication and management
   - SMIB acts as the "external host-controlled interface"

## What This Means for Our System

Instead of traditional WAT, we'll implement a **Player Account Management System** that:
- Uses the SMIB touchscreen as the player interface
- Leverages AFT for credit transfers
- Manages accounts entirely on the server side
- Provides WAT-like functionality without requiring G2S

This approach gives us more flexibility and better user experience than traditional SAS limitations would allow.