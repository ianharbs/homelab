using Jellyfin.Plugin.Share.Configuration;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace Jellyfin.Plugin.Share;

public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    public static readonly Guid PluginGuid = new("8f3c2a61-5d4e-4b7a-9c1e-2a6f0d9b7e41");

    public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
        : base(applicationPaths, xmlSerializer)
    {
        Instance = this;
    }

    public static Plugin? Instance { get; private set; }

    public override string Name => "Compartilhar";

    public override Guid Id => PluginGuid;

    public override string Description => "Links públicos (sem conta) para séries, temporadas, episódios e filmes.";

    public IEnumerable<PluginPageInfo> GetPages()
    {
        yield return new PluginPageInfo
        {
            Name = "Compartilhar",
            EmbeddedResourcePath = GetType().Namespace + ".Configuration.config.html",
        };
    }
}
