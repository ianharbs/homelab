# Preferência por edições brasileiras nos resultados do Z-Library.
#
# O Z-Library informa o idioma ("Brazilian" / "Portuguese" / ...) e a editora
# de cada livro, mas o Shelfarr descarta a editora e o ReleaseScorer só deduz
# o idioma pelo TÍTULO — livro em português sem marca no título ficava com
# nota de idioma 50 (máx. ~83) e nunca passava do limite de auto-seleção.
#
# Este initializer:
#   1. guarda a editora de cada resultado do Z-Library (provider_payload);
#   2. na nota de idioma, quando o pedido é em português:
#        editora de Portugal (Presença, Porto Editora...)  -> 20
#        "Brazilian" ou editora brasileira conhecida        -> 100
#        "Portuguese" sem editora identificável             -> 90
#        outro idioma informado pelo Z-Library              -> 0
#      para outros idiomas, usa o idioma do Z-Library quando o título não diz;
#   3. mantém o idioma do Z-Library em detected_language quando o título não
#      indica nenhum (a tela mostra a bandeira certa).
#
# Montado em /rails/config/initializers/ pelo docker-compose. Qualquer erro
# cai no comportamento original do Shelfarr.
Rails.application.config.after_initialize do
  module PtBrZlib
    # Editoras de Portugal: conferidas ANTES das brasileiras.
    PT_PUBLISHERS = [
      "presenca", "porto editora", "saida de emergencia", "bertrand editora",
      "livros do brasil", "edicoes asa", "asa editores", "gradiva", "quetzal",
      "dom quixote", "d. quixote", "relogio d'agua", "tinta-da-china", "tinta da china",
      "europa-america", "contraponto", "temas e debates", "casa das letras",
      "oficina do livro", "clube do autor", "topseller", "20|20", "nascente",
      "cultura editora", "marcador", "guerra e paz", "planeta manuscrito",
      "alfaguara portugal", "booksmile", "chiado", "editorial estampa", "leya portugal",
      "editorial caminho", "esfera dos livros", "suma de letras portugal", "objectiva"
    ].freeze

    BR_PUBLISHERS = [
      "rocco", "intrinseca", "companhia das letras", "companhia das letrinhas",
      "seguinte", "suma", "objetiva", "record", "galera", "bertrand brasil",
      "martins fontes", "harpercollins brasil", "harper collins brasil", "darkside",
      "aleph", "arqueiro", "sextante", "globo livros", "editora globo", "novo seculo",
      "nova fronteira", "planeta do brasil", "jangada", "verus", "best seller",
      "bestseller", "paralela", "zahar", "todavia", "ubu", "autentica", "valentina",
      "faro editorial", "plataforma21", "l&pm", "editora 34", "editora unesp",
      "boitempo", "morro branco", "cultrix", "jambo", "atica", "scipione", "saraiva",
      "melhoramentos", "wmf", "nemo", "pipoca & nanquim", "panini", "jbc", "newpop",
      "devir", "leya brasil", "casa da palavra", "fundamento", "tordesilhas",
      "editora pensamento", "vestigio", "gutenberg", "tusquets", "biblioteca azul",
      "cosac naify", "penguin-companhia", "penguin companhia",
      "alta books", "novatec", "casa do codigo", "elefante", "ediouro", "agir"
    ].freeze

    PT_CODES = %w[pt pt-br pt-pt].freeze

    module_function

    def normalize(text)
      I18n.transliterate(text.to_s).downcase.squish
    rescue StandardError
      text.to_s.downcase.squish
    end

    # Casa a editora como palavra/expressão inteira ("atica" não casa com
    # "matematica", "suma" não casa com "consumo").
    def publisher_regex(list)
      Regexp.new("(?<![[:alnum:]])(?:#{list.map { |p| Regexp.escape(p) }.join('|')})(?![[:alnum:]])")
    end

    # :pt (Portugal), :br (Brasil) ou nil
    def publisher_origin(publisher)
      name = normalize(publisher)
      return if name.blank?
      @pt_re ||= publisher_regex(PT_PUBLISHERS)
      @br_re ||= publisher_regex(BR_PUBLISHERS)
      return :pt if name.match?(@pt_re)
      return :br if name.match?(@br_re)
      nil
    end

    def remember_publishers(books)
      Array(books).each do |book|
        id = book["id"].to_s
        next if id.blank?
        Rails.cache.write("zlib_publisher:#{id}", book["publisher"].to_s.strip, expires_in: 2.days)
      end
    rescue StandardError => e
      Rails.logger.warn "[PtBrZlib] remember: #{e.class}: #{e.message}"
    end

    def publisher_for(result_id)
      Rails.cache.read("zlib_publisher:#{result_id}")
    rescue StandardError
      nil
    end

    # Nota de idioma (0-100) para um SearchResult do Z-Library, ou nil para
    # deixar a regra original decidir.
    def language_score(search_result, requested)
      return unless search_result.respond_to?(:source) && search_result.source == SearchResult::SOURCE_ZLIBRARY

      payload = search_result.provider_payload.is_a?(Hash) ? search_result.provider_payload : {}
      zlang = (payload["zlib_language"].presence || search_result.detected_language).to_s.downcase
      origin = publisher_origin(payload["publisher"])
      requested = requested.to_s.downcase

      if PT_CODES.include?(requested)
        return 20 if origin == :pt
        return 100 if zlang == "pt-br" || origin == :br
        return 90 if zlang == "pt"
        return 0 if zlang.present? && !PT_CODES.include?(zlang)
        nil
      elsif zlang.present?
        zlang == requested ? 100 : 0
      end
    end
  end

  # 1. Guarda a editora na busca da API (eapi) do Z-Library.
  ZLibraryClient.singleton_class.prepend(Module.new do
    private

    def parse_search_results(books, limit)
      PtBrZlib.remember_publishers(books)
      super
    end
  end)

  # 2. Grava editora e idioma do Z-Library no resultado salvo.
  SearchJob.prepend(Module.new do
    private

    def save_zlibrary_result(request, result)
      saved = super
      begin
        sr = saved.is_a?(SearchResult) ? saved : request.search_results.find_by(guid: "#{result.id}:#{result.hash}")
        if sr
          payload = (sr.provider_payload.is_a?(Hash) ? sr.provider_payload : {}).merge(
            "publisher" => PtBrZlib.publisher_for(result.id),
            "zlib_language" => result.language
          ).compact
          sr.update_columns(provider_payload: payload) if payload != sr.provider_payload
        end
      rescue StandardError => e
        Rails.logger.warn "[PtBrZlib] save: #{e.class}: #{e.message}"
      end
      saved
    end
  end)

  # 3. Nota de idioma usando idioma/editora do Z-Library.
  ReleaseScorer.prepend(Module.new do
    private

    def calculate_language_score
      original = super
      begin
        requested = @request.language || SettingsService.get(:default_language)
        # Só interfere quando o título não deu pista (nota neutra 50) ou
        # quando a editora é de Portugal num pedido em português.
        custom = PtBrZlib.language_score(@search_result, requested)
        return original if custom.nil?
        return custom if original == 50 || custom == 20
        original
      rescue StandardError => e
        Rails.logger.warn "[PtBrZlib] score: #{e.class}: #{e.message}"
        original
      end
    end
  end)

  # 3b. Trava contra "título = nome da série".
  # O ReleaseScorer dá título 100 sempre que o título pedido aparece como frase
  # inteira no nome do release. Quando o livro tem o mesmo nome da série
  # ("Trono de Vidro"), TODOS os volumes casam ("Coroa da Meia Noite: Trono de
  # vidro - Volume 2") e o auto-select escolhia qualquer um (15/11, pedido #64).
  # Se o release traz outro número de volume ou palavras a mais além do
  # título pedido, a nota de título cai para 60 (vai p/ seleção manual em vez
  # de baixar o livro errado).
  module PtBrTitleGuard
    VOLUME_WORDS = %w[vol volume livro book tomo parte part].freeze

    module_function

    def tokens(text)
      PtBrZlib.normalize(text).scan(/[[:alnum:]]+/)
    end

    def suspicious?(release_title, book)
      title = book.title.to_s
      return false if title.blank?

      author_tokens = tokens(book.author).select { |t| t.size >= 3 }
      main = release_title.to_s.sub(/(\s*(\[[^\]]*\]|\([^)]*\)))+\s*\z/, "")
      parts = main.split(/\s+-\s+/)
      parts.pop if parts.size > 1 && (tokens(parts.last) & author_tokens).any?
      main_tokens = tokens(parts.join(" "))
      title_tokens = tokens(title)

      extra = main_tokens - title_tokens - VOLUME_WORDS - author_tokens
      other_number = extra.any? { |t| t.match?(/\d/) && !t.match?(/\A0*1\z/) }
      return true if other_number

      # Qualquer palavra a mais ("Trono de vidro: Rainha das Sombras", "Box",
      # "Edição completa") vira seleção manual. Na dúvida, melhor perguntar do
      # que baixar o volume errado. O nome da série nem sempre vem no pedido,
      # então não dá para limitar isso só aos casos título = série.
      extra.any? { |t| !t.match?(/\d/) && t.size >= 3 }
    end
  end

  ReleaseScorer.prepend(Module.new do
    private

    def calculate_title_score
      original = super
      begin
        return original if original < 90
        PtBrTitleGuard.suspicious?(@search_result.title, @book) ? 60 : original
      rescue StandardError => e
        Rails.logger.warn "[PtBrTitleGuard] #{e.class}: #{e.message}"
        original
      end
    end
  end)

  # 4. Não apaga o idioma do Z-Library quando o título não indica nenhum.
  SearchResult.prepend(Module.new do
    def calculate_score!
      result = super
      begin
        if source == SearchResult::SOURCE_ZLIBRARY && detected_language.blank?
          zlang = provider_payload.is_a?(Hash) ? provider_payload["zlib_language"] : nil
          update_columns(detected_language: zlang) if zlang.present?
        end
      rescue StandardError => e
        Rails.logger.warn "[PtBrZlib] detected_language: #{e.class}: #{e.message}"
      end
      result
    end
  end)
end
