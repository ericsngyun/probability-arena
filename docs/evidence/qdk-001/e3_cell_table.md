| slice | cell | n_obs (tr/ho) | n_events (tr/ho) | TRAIN ΔS [95% CI] | HOLDOUT ΔS [CI] | BH adj p (ho) | verdict |
|---|---|---|---|---|---|---|---|
| 1. forecaster | baseball_evidence | 6239/3814 | 906/537 | -0.01668 [-0.02111, -0.01240] | -0.01556 [-0.01973, -0.01135] | 0.0007 | market better (BH-signif, ΔS<0) |
| 1. forecaster | soccer_evidence | 232/0 | 53/0 | — | — | — | **underpowered** |
| 2. sport / league | MLB | 6239/3814 | 906/537 | -0.01668 [-0.02111, -0.01240] | -0.01556 [-0.01973, -0.01135] | 0.0007 | market better (BH-signif, ΔS<0) |
| 2. sport / league | WC | 229/0 | 51/0 | — | — | — | **underpowered** |
| 2. sport / league | MLS | 3/0 | 2/0 | — | — | — | **underpowered** |
| 3. time-to-resolution | hours_to_close [-inf, 0.6594)h | 1294/803 | 605/345 | -0.04706 [-0.05657, -0.03754] | -0.04056 [-0.05077, -0.03031] | 0.0007 | market better (BH-signif, ΔS<0) |
| 3. time-to-resolution | hours_to_close [0.6594, 1.2698)h | 1294/833 | 593/349 | -0.01713 [-0.02694, -0.00766] | -0.01296 [-0.02138, -0.00454] | 0.0102 | market better (BH-signif, ΔS<0) |
| 3. time-to-resolution | hours_to_close [1.2698, 1.9422)h | 1293/772 | 586/367 | -0.01389 [-0.02121, -0.00688] | -0.00561 [-0.01446, +0.00391] | 0.2465 | no effect |
| 3. time-to-resolution | hours_to_close [1.9422, 2.5212)h | 1295/688 | 605/326 | -0.00960 [-0.01633, -0.00280] | -0.01210 [-0.01927, -0.00489] | 0.0051 | market better (BH-signif, ΔS<0) |
| 3. time-to-resolution | hours_to_close [2.5212, +inf)h | 1295/718 | 485/276 | -0.00156 [-0.00714, +0.00461] | -0.00464 [-0.01170, +0.00274] | 0.2280 | no effect |
| 4. market-probability decile | q [-inf, 0.065) | 604/272 | 363/173 | -0.03330 [-0.04192, -0.02564] | -0.02336 [-0.03131, -0.01522] | 0.0007 | market better (BH-signif, ΔS<0) |
| 4. market-probability decile | q [0.065, 0.16) | 677/394 | 406/230 | -0.01666 [-0.02797, -0.00577] | -0.01714 [-0.02580, -0.00757] | 0.0033 | market better (BH-signif, ΔS<0) |
| 4. market-probability decile | q [0.16, 0.24) | 639/441 | 383/262 | -0.01994 [-0.02884, -0.01115] | -0.00994 [-0.02195, +0.00249] | 0.1445 | no effect |
| 4. market-probability decile | q [0.24, 0.315) | 632/403 | 419/262 | -0.00298 [-0.01120, +0.00507] | -0.00856 [-0.01876, +0.00207] | 0.1445 | no effect |
| 4. market-probability decile | q [0.315, 0.39) | 676/399 | 453/275 | -0.00354 [-0.01110, +0.00412] | -0.00574 [-0.01460, +0.00390] | 0.2465 | no effect |
| 4. market-probability decile | q [0.39, 0.465) | 604/349 | 433/265 | -0.01084 [-0.02015, -0.00232] | -0.00561 [-0.01502, +0.00371] | 0.2465 | no effect |
| 4. market-probability decile | q [0.465, 0.555) | 686/375 | 431/242 | -0.00378 [-0.01176, +0.00408] | -0.00749 [-0.01788, +0.00275] | 0.1713 | no effect |
| 4. market-probability decile | q [0.555, 0.67) | 658/393 | 442/266 | -0.01056 [-0.02043, -0.00107] | -0.00556 [-0.01606, +0.00519] | 0.3082 | no effect |
| 4. market-probability decile | q [0.67, 0.82) | 647/381 | 409/259 | -0.02138 [-0.03439, -0.00887] | -0.01941 [-0.03272, -0.00640] | 0.0115 | market better (BH-signif, ΔS<0) |
| 4. market-probability decile | q [0.82, +inf) | 648/407 | 399/247 | -0.05732 [-0.07010, -0.04425] | -0.05350 [-0.06535, -0.04171] | 0.0007 | market better (BH-signif, ΔS<0) |
| 5. \|p-q\| disagreement | \|p-q\| [-inf, 0.0222) | 1293/712 | 561/340 | -0.00005 [-0.00063, +0.00052] | -0.00033 [-0.00120, +0.00057] | 0.4771 | no effect |
| 5. \|p-q\| disagreement | \|p-q\| [0.0222, 0.0538) | 1294/826 | 606/366 | -0.00102 [-0.00301, +0.00107] | -0.00194 [-0.00449, +0.00050] | 0.1575 | no effect |
| 5. \|p-q\| disagreement | \|p-q\| [0.0538, 0.0935) | 1295/791 | 605/357 | -0.00778 [-0.01150, -0.00394] | -0.00557 [-0.01052, -0.00047] | 0.0685 | market better (BH-signif, ΔS<0) |
| 5. \|p-q\| disagreement | \|p-q\| [0.0935, 0.16) | 1287/756 | 597/365 | -0.01156 [-0.01832, -0.00476] | -0.01136 [-0.01926, -0.00331] | 0.0171 | market better (BH-signif, ΔS<0) |
| 5. \|p-q\| disagreement | \|p-q\| [0.16, +inf) | 1302/729 | 603/345 | -0.06846 [-0.08671, -0.05008] | -0.06107 [-0.07661, -0.04538] | 0.0007 | market better (BH-signif, ΔS<0) |
| 6. spread | spread_avg [-inf, 1)c | 0/0 | 0/0 | — | — | — | **underpowered** |
| 6. spread | spread_avg [1, 1.3333)c | 2579/1473 | 832/485 | -0.02148 [-0.02793, -0.01522] | -0.01740 [-0.02347, -0.01131] | 0.0007 | market better (BH-signif, ΔS<0) |
| 6. spread | spread_avg [1.3333, 2.2)c | 1228/811 | 574/343 | -0.01534 [-0.02177, -0.00866] | -0.01448 [-0.02204, -0.00721] | 0.0017 | market better (BH-signif, ΔS<0) |
| 6. spread | spread_avg [2.2, 4.4)c | 1345/840 | 507/320 | -0.02045 [-0.02853, -0.01302] | -0.01911 [-0.02672, -0.01106] | 0.0007 | market better (BH-signif, ΔS<0) |
| 6. spread | spread_avg [4.4, +inf)c | 1319/690 | 470/273 | -0.01042 [-0.01754, -0.00369] | -0.00858 [-0.01642, -0.00055] | 0.0714 | market better (BH-signif, ΔS<0) |
| 7. depth / liquidity | liquidity_avg [-inf, 150010) | 1294/535 | 442/236 | -0.01584 [-0.02405, -0.00828] | -0.00788 [-0.01644, +0.00110] | 0.1092 | no effect |
| 7. depth / liquidity | liquidity_avg [150010, 580033) | 1294/962 | 479/297 | -0.01465 [-0.02197, -0.00769] | -0.01887 [-0.02618, -0.01178] | 0.0007 | market better (BH-signif, ΔS<0) |
| 7. depth / liquidity | liquidity_avg [580033, 1.31259e+06) | 1294/690 | 513/289 | -0.01827 [-0.02552, -0.01100] | -0.01593 [-0.02426, -0.00753] | 0.0012 | market better (BH-signif, ΔS<0) |
| 7. depth / liquidity | liquidity_avg [1.31259e+06, 3.07012e+06) | 1294/870 | 557/356 | -0.01648 [-0.02459, -0.00882] | -0.01027 [-0.01840, -0.00182] | 0.0358 | market better (BH-signif, ΔS<0) |
| 7. depth / liquidity | liquidity_avg [3.07012e+06, +inf) | 1295/757 | 477/312 | -0.02398 [-0.03393, -0.01420] | -0.02252 [-0.02974, -0.01506] | 0.0007 | market better (BH-signif, ΔS<0) |
| 8. favourite vs underdog | favourite (q > 0.5) | 2323/1385 | 789/469 | -0.02512 [-0.03259, -0.01775] | -0.02316 [-0.03064, -0.01556] | 0.0007 | market better (BH-signif, ΔS<0) |
| 8. favourite vs underdog | underdog (q <= 0.5) | 4148/2429 | 923/519 | -0.01377 [-0.01862, -0.00929] | -0.01123 [-0.01588, -0.00640] | 0.0007 | market better (BH-signif, ΔS<0) |
| 9. forecast direction | p > q (above market) | 3694/2190 | 900/511 | -0.01705 [-0.02382, -0.01083] | -0.00848 [-0.01431, -0.00217] | 0.0217 | market better (BH-signif, ΔS<0) |
| 9. forecast direction | p < q (below market) | 2711/1598 | 850/493 | -0.01936 [-0.02620, -0.01238] | -0.02552 [-0.03306, -0.01763] | 0.0007 | market better (BH-signif, ΔS<0) |
| 9. forecast direction | p == q (exact tie) | 66/26 | 27/22 | — | — | — | **underpowered** |
