@echo off
REM Atalho de 2 cliques: sobe o servidor local (via WSL) e abre o navegador.
REM Mantenha esta janela aberta enquanto usar a pagina. Feche-a para parar o servidor.
title TBH Market Tool - servidor
echo Iniciando o servidor local... (o navegador abre sozinho em alguns segundos)
echo Para parar, feche esta janela ou pressione Ctrl+C.
echo.
wsl -e bash -c "cd '/mnt/c/Users/filip/Downloads/PROJETO TBH TASK BAR HERO/tbh-market-tool' && python3 build.py serve"
echo.
echo Servidor encerrado.
pause
