# Maintenance Manual — Cooling Water Pump P-201

## Equipment: Grundfos CR 32-2

### 1. General Information

The Grundfos CR 32-2 is a vertical, multistage centrifugal pump designed for cooling water circulation. It is installed on Line 2 in Building A as part of the cooling system (Asset ID: P-201).

- **Flow rate**: 32 m³/h at design conditions
- **Head**: 24 m
- **Motor**: ABB M3BP 160MLB 4, 15 kW, 1450 rpm
- **Impeller material**: AISI 316 stainless steel
- **Seal type**: Mechanical seal, cartridge design

### 2. Preventive Maintenance Schedule

#### 2.1 Monthly Checks
- Check for abnormal noise or vibration
- Verify motor current is within nameplate limits (max 28.5 A)
- Inspect for visible leaks at mechanical seal
- Check bearing temperature (max 80°C)

#### 2.2 Quarterly Inspection (MP-P201-QUARTERLY)
1. **Vibration measurement** at both bearing points:
   - Drive End (DE): acceptable < 4.5 mm/s, alarm at 6.0 mm/s, trip at 9.0 mm/s
   - Non-Drive End (NDE): acceptable < 3.5 mm/s, alarm at 5.0 mm/s, trip at 7.5 mm/s
2. **Lubrication check**: Verify grease level in bearing housings. Re-grease with SKF LGMT 3 if needed (15g per bearing).
3. **Alignment verification**: Check coupling alignment using dial indicator or laser. Acceptable: < 0.05 mm offset, < 0.05 mm/100mm angular.
4. **Seal inspection**: Inspect mechanical seal area for leaks. Minor weeping (< 5 drops/min) is acceptable. Continuous flow indicates seal replacement needed.

#### 2.3 Annual Overhaul
- Full disassembly and inspection
- Replace bearings (SKF 6310 x2) regardless of condition
- Replace mechanical seal (part: SEAL-CR32-KIT)
- Inspect impeller for erosion or cavitation damage
- Check shaft runout (max 0.02 mm TIR)
- Hydrostatic test at 1.5x design pressure

### 3. Bearing Replacement Procedure

**Required parts**: SKF 6310 deep groove ball bearing (qty: 2)
**Required tools**: Bearing puller, induction heater, torque wrench
**Estimated time**: 3-4 hours
**Skills required**: Mechanical, Vibration analysis

#### Step-by-step:
1. Lock out / Tag out (LOTO) the motor. Verify zero energy state.
2. Disconnect coupling.
3. Remove bearing housing covers (4x M12 bolts, torque: 80 Nm).
4. Support shaft and use bearing puller to remove old bearings.
5. Clean shaft journals with solvent. Inspect for scoring (max 0.01 mm).
6. Heat new bearings to 110°C using induction heater.
7. Slide bearings onto shaft (interference fit: 0.013-0.028 mm).
8. Allow to cool naturally. Do NOT quench with water.
9. Pack bearing housings with SKF LGMT 3 grease (fill to 30-40% cavity).
10. Reassemble housing covers. Torque bolts to 80 Nm in star pattern.
11. Verify alignment.
12. Reconnect coupling.
13. Remove LOTO. Start pump and verify:
    - No abnormal noise
    - Vibration < 2.5 mm/s (new bearing acceptance criteria)
    - Bearing temperature stable < 60°C after 1 hour

### 4. Mechanical Seal Replacement Procedure

**Required parts**: SEAL-CR32-KIT
**Estimated time**: 2-3 hours
**Skills required**: Mechanical

1. LOTO the pump. Drain the casing.
2. Remove coupling and bearing housing.
3. Slide pump shaft assembly out of casing.
4. Remove old seal faces. Clean all seal surfaces.
5. Check shaft for scratches or wear at seal area (max 0.005 mm).
6. Install new seal following manufacturer assembly guide.
7. Lubricate O-rings with silicone grease.
8. Reassemble pump. Verify seal gap setting per manufacturer specs.
9. Fill casing. Vent air from top plug.
10. Start pump. Check for leaks at seal area (zero leak within 1 hour).

### 5. Troubleshooting Guide

| Symptom | Probable Cause | Action |
|---------|---------------|--------|
| High vibration (DE side) | Bearing wear | Measure vibration spectrum. If bearing frequencies present, plan bearing replacement. |
| High vibration (both sides) | Misalignment | Check and correct coupling alignment. |
| Seal leakage | Worn seal faces | Replace mechanical seal (SEAL-CR32-KIT). |
| Reduced flow | Impeller erosion | Inspect impeller. Replace if worn > 10%. |
| High motor current | Impeller blockage or cavitation | Check suction strainer. Verify NPSH available. |
| Elevated temperature | Insufficient lubrication | Re-grease bearings. Check grease type. |
| Noise (rattling) | Cavitation | Check suction conditions. Increase NPSH. |
| Noise (grinding) | Bearing failure (advanced) | EMERGENCY: Stop pump. Plan immediate replacement. |

### 6. Safety Warnings

⚠️ **Criticality A equipment** — This pump is critical for production Line 2. Unplanned downtime has direct production impact.

- Always follow LOTO procedures before any maintenance
- Wear appropriate PPE: safety glasses, gloves, steel-toe boots
- Ensure fire watch when performing hot work near pump
- Maximum coupling guard must be in place before starting
