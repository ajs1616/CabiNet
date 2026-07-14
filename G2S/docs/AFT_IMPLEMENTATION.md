# AFT (Automated Funds Transfer) Implementation Guide

This guide covers the enhanced AFT implementation in CabiNet, supporting multiple transfer types per G2S specifications.

## Transfer Types Supported

### 1. Standard Transfers
- **hostToEgm** - Transfer funds from host to slot machine
- **egmToHost** - Transfer funds from slot machine to host

### 2. Bonus Transfers
- **bonusToGame** - Transfer promotional/bonus funds
- Supports restricted and non-restricted amounts
- Configurable wagering requirements

### 3. Debit Transfers
- **debitToGame** - Transfer from debit card/account
- Optional PIN verification
- Authorization code generation

### 4. In-House Transfers
- **inHouseToGame** - Transfer from property account
- Player account integration
- Property-specific limits

### 5. Win Transfers
- **winToHost** - Transfer win amounts
- **jackpotToHost** - Transfer jackpot winnings

## Configuration

Edit `config/g2s_config.json`:

```json
{
  "g2s_features": {
    "aft": {
      "enabled": true,
      "bonus_transfers": true,
      "debit_transfers": true,
      "in_house_transfers": true,
      "max_transaction_amount": 100000,  // $1,000.00
      "min_transaction_amount": 100,     // $1.00
      "require_pin": false,
      "timeout_seconds": 30,
      "partial_transfers": true,
      "lock_timeout": 30000,
      "max_pending_transactions": 10
    }
  }
}
```

## Transaction Flow

### 1. Begin Transaction

```xml
POST /G2S
Content-Type: text/xml

<?xml version="1.0" encoding="UTF-8"?>
<g2s:g2sMessage xmlns:g2s="http://www.gamingstandards.com/g2s/schemas/v1.0.3"
    egmId="IGT-001"
    hostId="CASINONET-001"
    sessionId="12345"
    sessionType="G2S_request"
    dateTime="2025-07-24T10:00:00Z">
    <g2s:beginAftTransaction
        transferType="bonusToGame"
        transferAmount="5000"
        bonusText="Welcome Bonus"
        restrictedAmount="5000"
        nonRestrictedAmount="0"
        wageringRequirements="30"
    />
</g2s:g2sMessage>
```

### 2. Transaction Response

```xml
<?xml version="1.0" encoding="UTF-8"?>
<g2s:g2sMessage xmlns:g2s="http://www.gamingstandards.com/g2s/schemas/v1.0.3">
    <g2s:beginAftTransactionAck 
        transactionId="AFT-A1B2C3D4E5F6"
        transferStatus="authorized"
        authorizationCode="AUTH-12345678"
    />
</g2s:g2sMessage>
```

### 3. Commit Transaction

```xml
<g2s:commitAftTransaction
    transactionId="AFT-A1B2C3D4E5F6"
/>
```

### 4. Commit Response

```xml
<g2s:commitAftTransactionAck 
    transactionId="AFT-A1B2C3D4E5F6"
    transferStatus="committed">
    <g2s:meterDelta meterId="bonusIn" meterDelta="5000" />
    <g2s:meterDelta meterId="restrictedCredits" meterDelta="5000" />
</g2s:commitAftTransactionAck>
```

## Transfer Type Examples

### Bonus Transfer

```xml
<g2s:beginAftTransaction
    transferType="bonusToGame"
    transferAmount="10000"
    bonusText="Friday Special - 100% Match"
    restrictedAmount="10000"
    nonRestrictedAmount="0"
    wageringRequirements="35"
/>
```

**Features:**
- Bonus text displayed to player
- Restricted funds require wagering
- Non-restricted funds immediately cashable
- Wagering requirement multiplier

### Debit Transfer

```xml
<g2s:beginAftTransaction
    transferType="debitToGame"
    transferAmount="50000"
    accountNumber="************1234"
    pin="ENCRYPTED_PIN_HASH"
/>
```

**Features:**
- Masked account number
- Encrypted PIN transmission
- Authorization code returned
- Real-time validation

### In-House Transfer

```xml
<g2s:beginAftTransaction
    transferType="inHouseToGame"
    transferAmount="20000"
    accountId="PLAYER-98765"
    propertyId="VEGAS-001"
    playerName="John Doe"
/>
```

**Features:**
- Property account integration
- Player identification
- Cross-property support
- Balance verification

## Transaction States

1. **pending** - Initial state after beginAftTransaction
2. **authorized** - Payment authorized, awaiting commit
3. **committed** - Transfer completed successfully
4. **cancelled** - Transfer cancelled by user/timeout
5. **failed** - Transfer failed (insufficient funds, etc.)
6. **timeout** - Transaction expired

## Error Handling

### Insufficient Funds
```xml
<g2s:error errorCode="G2S_AFT001" 
    errorText="Insufficient funds in account" />
```

### Invalid Transfer Type
```xml
<g2s:error errorCode="G2S_AFT002" 
    errorText="Transfer type not supported" />
```

### Transaction Timeout
```xml
<g2s:error errorCode="G2S_AFT003" 
    errorText="Transaction timeout - please retry" />
```

## Meter Updates

AFT transactions update various meters:

| Transfer Type | Meters Updated |
|--------------|----------------|
| hostToEgm | cashIn |
| bonusToGame | bonusIn, restrictedCredits, nonRestrictedCredits |
| debitToGame | cashIn, electronicIn |
| inHouseToGame | cashIn, watIn |
| egmToHost | cashOut |

## Security Considerations

1. **Transaction Limits**
   - Configurable min/max amounts
   - Daily limits per player
   - Property-specific limits

2. **Authentication**
   - PIN verification for debit
   - Player card for in-house
   - Session-based security

3. **Authorization**
   - Real-time balance checks
   - Fraud detection
   - Velocity limits

4. **Audit Trail**
   - All transactions logged
   - Immutable transaction history
   - Regulatory compliance

## Testing AFT Transfers

### 1. Test Bonus Transfer
```bash
curl -X POST http://localhost:8081/G2S \
  -H "Content-Type: text/xml" \
  -d '<?xml version="1.0" encoding="UTF-8"?>
<g2s:g2sMessage xmlns:g2s="http://www.gamingstandards.com/g2s/schemas/v1.0.3"
    egmId="TEST-001">
    <g2s:beginAftTransaction
        transferType="bonusToGame"
        transferAmount="5000"
        bonusText="Test Bonus"
    />
</g2s:g2sMessage>'
```

### 2. Check Transaction Status

Every transfer lands in the web UI's Wallet ledger (and `/api/accounts`);
SAS-side AFT state is in the smib journal (`journalctl -u casinonet-sas`).

### 3. Run Automated Tests
```bash
python3 G2S/tools/test_hub_store.py     # hub money/ledger gate
pytest SAS/ -q                          # includes the AFT host state machine
```

## Troubleshooting

### AFT Not Enabled
Check `config/g2s_config.json` and ensure AFT is enabled.

### Transaction Fails
1. Check transaction limits
2. Verify transfer type is enabled
3. Check account balance (for debit/in-house)
4. Review logs in `data/g2s_debug.log`

### Meter Updates Missing
Ensure transaction is committed (not just authorized).

## Best Practices

1. **Always commit or cancel** - Don't leave transactions pending
2. **Set reasonable timeouts** - Default 30 seconds
3. **Implement retry logic** - Handle network failures
4. **Validate amounts** - Check limits before sending
5. **Log everything** - Maintain audit trail
6. **Test thoroughly** - Use test mode before production