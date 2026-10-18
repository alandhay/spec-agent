10 REM primes below 100
20 BORDER 1: PAPER 7: INK 0: CLS
30 PRINT INVERSE 1;"     PRIME NUMBERS BELOW 100    "
40 PRINT
50 LET count=0
60 FOR n=2 TO 99
70 LET prime=1
80 FOR d=2 TO INT SQR n
90 IF n=d*INT (n/d) THEN LET prime=0: LET d=n
100 NEXT d
110 IF prime=0 THEN GO TO 150
120 LET col=count-5*INT (count/5)
130 PRINT TAB col*6+3-LEN STR$ n;n;
140 LET count=count+1: IF col=4 THEN PRINT
150 NEXT n
160 PRINT
170 PRINT "Found ";count;" primes"
