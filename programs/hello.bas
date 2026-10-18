10 REM specbot smoke test
20 BORDER 1: PAPER 7: INK 0: CLS
30 PRINT AT 2,4; INVERSE 1;" HELLO FROM SPECBOT "
40 FOR n=1 TO 5: PRINT n;TAB 6;n*n;TAB 12;n/4: NEXT n
50 DEF FN s(x,y)=x*x+y
60 PRINT "FN s(3,1.5)=";FN s(3,1.5);" BIN=";BIN 1010
70 LET a$="score": PRINT a$;" £";1e3;" ";.5
80 PRINT undefined
