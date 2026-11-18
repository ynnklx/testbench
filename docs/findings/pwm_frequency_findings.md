# ESC PWM Frequency vs. Electrical Power

1800 KV motor, 5x3.7x3 propeller, 20 V. One run each at 12, 24 and 48 kHz.

![Electrical power vs. thrust at 12 / 24 / 48 kHz](data/plots/20260822_pwm_comparison_thrust.png)

## Result

Electrical power at equal thrust, compared to 12 kHz:

| Thrust | 24 kHz | 48 kHz |
|---|---|---|
| 100 g | -26 % | -28 % |
| 200 g | -17 % | -20 % |
| 300 g | -14 % | -14 % |
| 400 g | -12 % | n/a |

Going from 12 to 24 kHz saves a lot of power. Doubling again to 48 kHz adds little. At 200 g, the PWM effect is larger than the effect of KV (about 15 %) or supply voltage (about 6 %) in the main test series.

## Possible cause

PWM current ripple in the motor windings, which decreases with frequency and adds losses without producing torque. Not verified, since only the DC input side of the ESC is measured.

## Consequence

The main test series runs at a fixed 48 kHz.
