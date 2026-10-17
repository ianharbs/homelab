# octo-fiesta upstream + report-playback.patch: intercepta /rest/reportPlayback (OpenSubsonic, Navidrome >= 0.64)
# para faixas externas, que o Feishin mostrava como "data not found". Issue upstream: V1ck3s/octo-fiesta#332.
# Quando o upstream corrigir, volte o compose para a imagem ghcr.io/v1ck3s/octo-fiesta:latest e apague esta pasta.
# Para atualizar sem isso: troque OCTO_REF pelo commit novo e confira se o patch ainda aplica.
ARG OCTO_REF=e15c0c72acb2168d1ec98ec90b7e6b31d50f6f14

FROM mcr.microsoft.com/dotnet/sdk:9.0 AS build
ARG OCTO_REF
WORKDIR /src
RUN git clone --filter=blob:none https://github.com/V1ck3s/octo-fiesta.git . && git checkout --quiet "$OCTO_REF"
COPY report-playback.patch /tmp/
RUN git apply /tmp/report-playback.patch
RUN dotnet publish octo-fiesta/octo-fiesta.csproj -c Release -p:Version=0.0.0-homelab -o /app/publish

FROM mcr.microsoft.com/dotnet/aspnet:9.0
WORKDIR /app
RUN mkdir -p /app/downloads
COPY --from=build /app/publish .
EXPOSE 8080
ENV ASPNETCORE_URLS=http://+:8080
ENTRYPOINT ["dotnet", "octo-fiesta.dll"]
