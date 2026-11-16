using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.Share.Configuration;

public class PluginConfiguration : BasePluginConfiguration
{
    /// <summary>URL pública usada para montar os links (ex.: https://jf.ihome.dev.br). Vazio = endereço do navegador de quem criou.</summary>
    public string PublicBaseUrl { get; set; } = string.Empty;

    /// <summary>Validade sugerida no diálogo (dias, 0 = sem validade).</summary>
    public int DefaultDays { get; set; } = 7;

    /// <summary>Se true, só administradores veem o botão e podem criar links.</summary>
    public bool OnlyAdmins { get; set; } = true;

    /// <summary>Reproduções simultâneas por link (cada uma pode abrir uma transcodificação).</summary>
    public int MaxStreamsPerLink { get; set; } = 3;
}
