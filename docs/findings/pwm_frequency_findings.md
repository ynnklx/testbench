# ESC PWM Frequency vs. Electrical Power

1800 KV motor, 5x3.7x3 propeller, T-Hobby F35A ESC (AM32 firmware), 20 V. One run each at 12, 24 and 48 kHz.

Earlier runs showed irregular, poorly reproducible results. I traced them back to the ESC's *Variable PWM* setting, which changes the switching frequency during operation. This led to a dedicated test of how the PWM frequency affects electrical power and efficiency.

![Electrical power vs. thrust at 12 / 24 / 48 kHz](../data/plots/20260822_pwm_comparison_thrust.png)

## Result

Electrical power at equal thrust, change vs. 12 kHz in brackets:

| Thrust | 12 kHz | 24 kHz | 48 kHz |
|---|---|---|---|
| 100 g | 21.2 W | 15.8 W (-26 %) | 15.2 W (-28 %) |
| 200 g | 46.7 W | 38.8 W (-17 %) | 37.5 W (-20 %) |
| 300 g | 76.0 W | 65.6 W (-14 %) | 65.3 W (-14 %) |
| 400 g | 110.4 W | 97.6 W (-12 %) | n/a |

*Values linearly interpolated between the measured throttle stages.*

Going from 12 to 24 kHz saves a lot of power. Doubling again to 48 kHz adds little. At 200 g, the PWM effect is larger than the effect of KV (about 15 %) or supply voltage (about 6 %) in the main test series.

## Possible cause

PWM current ripple in the motor windings, which decreases with frequency and adds losses without producing torque. Not verified, since only the DC input side of the ESC is measured.

## Consequence

The main test series runs at a fixed 48 kHz.
