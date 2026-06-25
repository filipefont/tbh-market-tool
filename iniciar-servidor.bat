@echo off
REM Atalho de 2 cliques: sobe o servidor local (via WSL) e abre o navegador.
REM Mantenha esta janela aberta enquanto usar a pagina. Feche-a para parar o servidor.
title TBH Market Tool - servidor
echo Iniciando o servidor local... (o navegador abre sozinho em alguns segundos)
echo Para parar, feche esta janela ou pressione Ctrl+C.
echo.
REM abre o navegador em ~3s (o servidor sobe na sequencia e atende na porta 8765)
start "" cmd /c "timeout /t 3 >nul & start http://localhost:8765/"
wsl -e bash -c "cd '/mnt/c/Users/filip/OneDrive/Documentos/GitHub/tbh-market-tool' && python3 build.py serve"
echo.
echo Servidor encerrado.
pause
